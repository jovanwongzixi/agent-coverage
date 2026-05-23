from __future__ import annotations

import argparse
from typing import Optional
import os
import re
import shlex
import tempfile
import subprocess
from dataclasses import dataclass, field

import json


MAX_COVERAGE_FIX_ATTEMPTS = 3
MAX_COMMANDS_PER_COVERAGE_CHUNK = 100
SHELL_OPERATOR_TOKENS = {'&&', '||', ';', '|'}
SED_PRINT_RANGE_RE = re.compile(r'^(?P<start>\d+),(?P<end>\d+)p$')
SHELL_VAR_RE = re.compile(
    r'^\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)$|^\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}$'
)


coverage_prompt = (
    "The current working directory contains exactly one file, "
    "coverage.json. Update that file in place.\n"
    "coverage.json contains a JSON array of objects shaped like "
    "{\"cmd\": string, \"ranges\": string[]}.\n"
    "For each command, populate ranges with the relevant source-code "
    "line spans as strings in the form path:start:end.\n"
    "This is to know which files a coding agent was interested in "
    "reading.\n"
    "Examples (non-exaustive, can be more complex):\n"
    "- Straight forward read file command:"
    "`{\"cmd\":\"sed -n '260,520p' src/StabilityPool.sol\","
    "\"ranges\":[\"src/StabilityPool.sol:260:520\"]}`.\n"
    "- Can be chained commands with pipes"
    "`{\"cmd\":\"nl -ba src/TroveManager.sol | sed -n '900,980p'\","
    "\"ranges\":[\"src/TroveManager.sol:900:980\"]}`.\n"
    "- If cmd is a loop reading multiple files, list all the relevant file ranges."
    "`{\"cmd\":\"for f in triaged/H-1.md triaged/H-2.md "
    "triaged/M-1.md triaged/M-2.md triaged/M-3.md triaged/M-4.md "
    "triaged/M-5.md; do echo \\\"===== $(basename \\\"$f\\\") "
    "=====\\\"; sed -n '1,220p' \\\"$f\\\"; echo; done\","
    "\"ranges\":[\"triaged/H-1.md:1:220\","
    "\"triaged/H-2.md:1:220\",\"triaged/M-1.md:1:220\","
    "\"triaged/M-2.md:1:220\",\"triaged/M-3.md:1:220\","
    "\"triaged/M-4.md:1:220\",\"triaged/M-5.md:1:220\"]}`.\n"
    "- If cmd uses an absolute file path inside a git repo, make the range path "
    "repo-relative rather than absolute."
    "`{\"cmd\":\"sed -n '1,20p' /tmp/repo/src/app.ts\","
    "\"ranges\":[\"src/app.ts:1:20\"]}`.\n"
    "- Some commands don't read file ranges, or only superficially "
    " in these cases the ranges array should be empty."
    "`{\"cmd\":\"rg -n \\\"address\\\\(this\\\\).*remove "
    "manager|_isRemoveManagerAndReceiver|setAddManager|"
    "setRemoveManager|owner,\\\" src/Zappers -g '*.sol'\","
    "\"ranges\":[]}`.\n"
    "There might not be many commands, that have ranges, since simple commands "
    "might be preparsed to save context. "
    "Don't worry if the ranges overlap, just output overlapping ranges.\n"
    "Keep the array length the same, preserve every cmd string exactly once "
    "and in the same order, ensure every ranges value uses the form "
    "path:start:end with positive integer line numbers and start <= end, "
    "and preserve valid JSON."
)


@dataclass
class ShellSimpleCommand:
    """Represents a simple shell command after tokenization."""
    words: list[str]


@dataclass
class ShellPipeline:
    """Represents a shell pipeline."""
    commands: list[ShellSimpleCommand]


@dataclass
class ShellSequence:
    """Represents commands chained with shell operators like && or ;."""
    parts: list['ShellNode']
    operators: list[str]


@dataclass
class ShellForLoop:
    """Represents a simple `for name in ...; do ...; done` shell loop."""
    var_name: str
    items: list[str]
    body: ShellSequence


ShellNode = ShellSimpleCommand | ShellPipeline | ShellSequence | ShellForLoop


@dataclass
class ChecklistItem:
    """Represents a parsed checklist step from a planning update."""
    step: str
    status: str


@dataclass
class SessionTaskNode:
    """Represents a hierarchical task, commentary, or command node."""
    kind: str
    prompt: str
    commands: list[str] = field(default_factory=list)
    children: list['SessionTaskNode'] = field(default_factory=list)
    subagent_ids: list[str] = field(default_factory=list)
    status: Optional[str] = None
    synthetic: bool = False


def _extend_unique(values: list[str], new_values: list[str]) -> list[str]:
    """Appends unique non-empty strings while preserving order."""
    for value in new_values:
        if value and value not in values:
            values.append(value)
    return values


def _create_command_node(command: str) -> SessionTaskNode:
    """Creates a leaf node for a single coverage-bearing command."""
    return SessionTaskNode(kind='command', prompt=command, commands=[command])


def _append_command_node(parent: SessionTaskNode, command: str) -> None:
    """Appends a command leaf below the provided parent node."""
    parent.children.append(_create_command_node(command))


def _append_or_reuse_child(
    parent: SessionTaskNode,
    *,
    kind: str,
    prompt: str,
    status: Optional[str] = None,
) -> SessionTaskNode:
    """Appends a child node unless the previous sibling is the same empty node."""
    if parent.children:
        last_child = parent.children[-1]
        if (
            last_child.kind == kind
            and last_child.prompt == prompt
            and last_child.status == status
            and not last_child.commands
            and not last_child.children
        ):
            return last_child

    node = SessionTaskNode(kind=kind, prompt=prompt, status=status)
    parent.children.append(node)
    return node


def _merge_session_node(target: SessionTaskNode, source: SessionTaskNode) -> None:
    """Merges a parsed child node into an existing placeholder node."""
    if not target.prompt and source.prompt:
        target.prompt = source.prompt
    if source.status and not target.status:
        target.status = source.status
    target.synthetic = target.synthetic or source.synthetic

    _extend_unique(target.commands, source.commands)
    _extend_unique(target.subagent_ids, source.subagent_ids)
    target.children.extend(source.children)


class _ShellParser:
    """Parses a useful subset of shell syntax for command coverage extraction."""

    def __init__(self, command: str):
        lexer = shlex.shlex(command, posix=True, punctuation_chars='();|&')
        lexer.whitespace_split = True
        lexer.commenters = ''
        self.tokens = list(lexer)
        self.index = 0

    def parse(self) -> ShellSequence:
        if not self.tokens:
            raise ValueError('Shell command is empty.')

        sequence = self._parse_sequence(set())
        if self._peek() is not None:
            raise ValueError(f'Unexpected token {self._peek()!r}.')
        return sequence

    def _peek(self) -> Optional[str]:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def _consume(self, expected: Optional[str] = None) -> str:
        token = self._peek()
        if token is None:
            raise ValueError('Unexpected end of shell command.')
        if expected is not None and token != expected:
            raise ValueError(f'Expected token {expected!r}, got {token!r}.')
        self.index += 1
        return token

    def _parse_sequence(self, stop_tokens: set[str]) -> ShellSequence:
        parts: list[ShellNode] = []
        operators: list[str] = []

        while True:
            token = self._peek()
            if token is None or token in stop_tokens:
                break

            parts.append(self._parse_compound(stop_tokens))

            token = self._peek()
            if token is None or token in stop_tokens:
                break
            if token not in {'&&', '||', ';'}:
                raise ValueError(f'Unsupported shell operator {token!r}.')

            operator = self._consume()
            if operator == ';' and (self._peek() is None or self._peek() in stop_tokens):
                break
            operators.append(operator)

        if not parts:
            raise ValueError('Expected at least one shell command.')

        return ShellSequence(parts, operators)

    def _parse_compound(self, stop_tokens: set[str]) -> ShellNode:
        if self._peek() == 'for':
            return self._parse_for_loop(stop_tokens)
        return self._parse_pipeline(stop_tokens)

    def _parse_pipeline(self, stop_tokens: set[str]) -> ShellNode:
        commands = [self._parse_simple_command(stop_tokens | {'|'})]
        while self._peek() == '|':
            self._consume('|')
            commands.append(self._parse_simple_command(stop_tokens | {'|'}))

        if len(commands) == 1:
            return commands[0]
        return ShellPipeline(commands)

    def _parse_simple_command(self, stop_tokens: set[str]) -> ShellSimpleCommand:
        words: list[str] = []
        while True:
            token = self._peek()
            if token is None or token in stop_tokens or token in SHELL_OPERATOR_TOKENS:
                break
            words.append(self._consume())

        if not words:
            raise ValueError('Expected a simple shell command.')
        return ShellSimpleCommand(words)

    def _parse_for_loop(self, stop_tokens: set[str]) -> ShellForLoop:
        self._consume('for')
        var_name = self._consume()
        if not var_name.isidentifier():
            raise ValueError(f'Unsupported shell loop variable {var_name!r}.')

        self._consume('in')

        items: list[str] = []
        while True:
            token = self._peek()
            if token is None:
                raise ValueError('Unexpected end of shell loop.')
            if token == 'do':
                break
            if token == ';':
                self._consume(';')
                if self._peek() == 'do':
                    break
                raise ValueError('Unexpected token after loop item list.')
            items.append(self._consume())

        self._consume('do')
        body = self._parse_sequence(stop_tokens | {'done'})
        self._consume('done')
        return ShellForLoop(var_name, items, body)


def _parse_sed_print_script(script: str) -> Optional[list[tuple[int, int]]]:
    """Parses a sed print script like `1,20p;40,60p` into line ranges."""
    ranges: list[tuple[int, int]] = []
    for raw_clause in script.split(';'):
        clause = raw_clause.strip()
        if not clause:
            continue

        match = SED_PRINT_RANGE_RE.fullmatch(clause)
        if match is None:
            return None

        start = int(match.group('start'))
        end = int(match.group('end'))
        if start < 1 or end < 1 or start > end:
            return None
        ranges.append((start, end))

    return ranges or None


def _looks_like_literal_loop_item(item: str) -> bool:
    """Returns whether a loop item is a literal path rather than shell expansion."""
    return not any(marker in item for marker in {'$', '*', '?', '[', ']', '{', '}'})


def _resolve_path_token(
    token: str, loop_vars: dict[str, str]
) -> Optional[list[str]]:
    """Resolves a shell path token, including a simple loop variable reference."""
    match = SHELL_VAR_RE.fullmatch(token)
    if match is not None:
        var_name = match.group('plain') or match.group('braced')
        resolved = loop_vars.get(var_name)
        return [resolved] if resolved else None

    if token.startswith('$') or any(marker in token for marker in {'*', '?', '[', ']'}):
        return None
    return [token]


def _ranges_to_specs(paths: list[str], line_ranges: list[tuple[int, int]]) -> list[str]:
    """Formats resolved file paths and line tuples into path:start:end specs."""
    return [
        f'{path}:{start}:{end}'
        for path in paths
        for start, end in line_ranges
    ]


def _extract_sed_command_ranges(
    words: list[str],
    loop_vars: dict[str, str],
) -> Optional[list[str]]:
    """Extracts coverage ranges from a supported `sed -n` file read."""
    if len(words) < 3 or words[0] != 'sed' or words[1] != '-n':
        return None

    line_ranges = _parse_sed_print_script(words[2])
    if line_ranges is None:
        return None

    path_words = list(words[3:])
    if not path_words:
        return []
    if path_words[0] == '--':
        path_words = path_words[1:]
    if len(path_words) != 1:
        return None

    paths = _resolve_path_token(path_words[0], loop_vars)
    if paths is None:
        return None
    return _ranges_to_specs(paths, line_ranges)


def _extract_paged_file_pipeline_ranges(
    producer_words: list[str],
    pager_words: list[str],
    loop_vars: dict[str, str],
) -> Optional[list[str]]:
    """Extracts ranges from pipelines like `nl -ba file | sed -n '10,20p'`."""
    pager_ranges = _extract_sed_command_ranges(pager_words, loop_vars)
    if pager_ranges is None or pager_ranges:
        return None

    path_words: list[str]
    if producer_words[:2] == ['nl', '-ba']:
        path_words = producer_words[2:]
    elif producer_words[:1] == ['cat']:
        path_words = producer_words[1:]
    else:
        return None

    if path_words and path_words[0] == '--':
        path_words = path_words[1:]
    if len(path_words) != 1:
        return None

    path_values = _resolve_path_token(path_words[0], loop_vars)
    if path_values is None:
        return None

    line_ranges = _parse_sed_print_script(pager_words[2])
    if line_ranges is None:
        return None
    return _ranges_to_specs(path_values, line_ranges)


def _extract_simple_command_ranges(
    command: ShellSimpleCommand,
    loop_vars: dict[str, str],
) -> Optional[list[str]]:
    """Extracts ranges for a supported simple command or [] for safe no-range commands."""
    words = command.words
    if not words:
        return None

    sed_ranges = _extract_sed_command_ranges(words, loop_vars)
    if sed_ranges is not None:
        return sed_ranges

    head = words[0]
    if head in {'pwd', 'echo', 'printf', 'true', 'false', ':', 'find', 'which', 'ls', 'sort', 'wc'}:
        return []
    if head in {'rg', 'grep', 'egrep', 'fgrep'}:
        return []
    if head in {'head', 'tail'} and all(
        word.startswith('-') or word.isdigit() for word in words[1:]
    ):
        return []
    if head == 'command' and words[1:2] == ['-v']:
        return []
    if head == 'git' and words[1:2] and words[1] in {'status', 'diff', 'rev-parse'}:
        return []
    if head in {'test', '['}:
        return []
    if '--version' in words[1:] and len(words) <= 3:
        return []
    return None


def _extract_shell_ranges(
    node: ShellNode,
    loop_vars: Optional[dict[str, str]] = None,
) -> Optional[list[str]]:
    """Walks a parsed shell AST and extracts deterministic coverage ranges."""
    current_loop_vars = loop_vars or {}

    if isinstance(node, ShellSimpleCommand):
        return _extract_simple_command_ranges(node, current_loop_vars)

    if isinstance(node, ShellPipeline):
        if len(node.commands) == 2:
            pipeline_ranges = _extract_paged_file_pipeline_ranges(
                node.commands[0].words,
                node.commands[1].words,
                current_loop_vars,
            )
            if pipeline_ranges is not None:
                return pipeline_ranges

        ranges: list[str] = []
        for command in node.commands:
            command_ranges = _extract_simple_command_ranges(command, current_loop_vars)
            if command_ranges is None:
                return None
            ranges.extend(command_ranges)
        return ranges

    if isinstance(node, ShellForLoop):
        if any(not _looks_like_literal_loop_item(item) for item in node.items):
            return None

        loop_ranges: list[str] = []
        for item in node.items:
            nested_loop_vars = dict(current_loop_vars)
            nested_loop_vars[node.var_name] = item
            body_ranges = _extract_shell_ranges(node.body, nested_loop_vars)
            if body_ranges is None:
                return None
            loop_ranges.extend(body_ranges)
        return loop_ranges

    if isinstance(node, ShellSequence):
        if not node.parts:
            return []

        collected: list[str] = []
        for index, part in enumerate(node.parts):
            part_ranges = _extract_shell_ranges(part, current_loop_vars)
            if part_ranges is None:
                return None

            if index > 0 and node.operators[index - 1] == '||':
                if collected or part_ranges:
                    return None
                continue

            collected.extend(part_ranges)
        return collected

    return None


def _parse_command_ranges(command: str) -> Optional[list[str]]:
    """Parses a shell command into deterministic coverage ranges when possible."""
    try:
        parsed_command = _ShellParser(command).parse()
    except ValueError:
        return None
    return _extract_shell_ranges(parsed_command)


def _load_session_data(session_file_path: str) -> list[dict]:
    """Loads a session file containing concatenated JSON objects."""
    with open(session_file_path, 'r') as f:
        raw = f.read()

    decoder = json.JSONDecoder()
    session_data: list[dict] = []
    idx = 0

    while idx < len(raw):
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
        if idx >= len(raw):
            break

        item, idx = decoder.raw_decode(raw, idx)
        if isinstance(item, list):
            if any(not isinstance(entry, dict) for entry in item):
                raise ValueError('Session arrays must only contain JSON objects.')
            session_data.extend(item)
            continue
        if not isinstance(item, dict):
            raise ValueError('Session files must contain JSON objects.')
        session_data.append(item)

    return session_data


def _detect_session_format(session_data: list[dict]) -> str:
    """Returns the session format for the loaded event stream."""
    for data in session_data[:25]:
        event_type = data.get('type')
        if event_type in {'session_meta', 'response_item', 'event_msg'}:
            return 'codex'
        if event_type in {
            'assistant',
            'user',
            'progress',
            'system',
            'queue-operation',
            'file-history-snapshot',
            'last-prompt',
        }:
            return 'claude'
    raise ValueError('Unsupported session format.')


def _is_subagent_session(session_data: list[dict]) -> bool:
    """Returns whether the session was started as a subagent."""
    is_subagent = False
    for data in session_data:
        if data.get('type') != 'session_meta':
            continue
        source = data.get('payload', {}).get('source')
        is_subagent = isinstance(source, dict) and 'subagent' in source
    return is_subagent


def _extract_request_text(data: dict) -> Optional[str]:
    """Extracts a real user request from an event or falls back to older shapes."""
    if data.get('type') == 'event_msg' and data.get('payload', {}).get('type') == 'user_message':
        message = data.get('payload', {}).get('message', '').strip()
        return message or None

    payload = data.get('payload', {})
    if data.get('type') != 'response_item':
        return None
    if payload.get('type') != 'message' or payload.get('role') != 'user':
        return None

    texts = [
        content.get('text', '').strip()
        for content in payload.get('content', [])
        if content.get('type') == 'input_text' and content.get('text', '').strip()
    ]
    if not texts:
        return None

    text = texts[-1]
    if (
        text.startswith('# AGENTS.md')
        or text.startswith('<environment_context>')
        or text.startswith('<subagent_notification>')
    ):
        return None
    return text


def _extract_subagent_notification_id(data: dict) -> Optional[str]:
    """Extracts an agent ID from a subagent notification message."""
    payload = data.get('payload', {})
    if data.get('type') != 'response_item':
        return None
    if payload.get('type') != 'message' or payload.get('role') != 'user':
        return None

    for content in payload.get('content', []):
        text = content.get('text', '')
        if content.get('type') != 'input_text' or not text.startswith('<subagent_notification>'):
            continue
        try:
            return json.loads(text.split('\n', 2)[1]).get('agent_id')
        except (IndexError, json.JSONDecodeError):
            return None
    return None


def _extract_codex_spawn_agent_prompt(payload: dict) -> Optional[str]:
    """Extracts the spawned subagent prompt from a Codex spawn_agent call."""
    if payload.get('type') != 'function_call' or payload.get('name') != 'spawn_agent':
        return None

    try:
        arguments = json.loads(payload.get('arguments', '{}'))
    except json.JSONDecodeError:
        return None

    prompt = arguments.get('message')
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()

    items = arguments.get('items')
    if not isinstance(items, list):
        return None

    texts = [
        item.get('text', '').strip()
        for item in items
        if isinstance(item, dict)
        and item.get('type') == 'text'
        and item.get('text', '').strip()
    ]
    if not texts:
        return None
    return '\n'.join(texts)


def _extract_checklist_items(raw_plan: object) -> list[ChecklistItem]:
    """Extracts checklist items from a structured plan payload."""
    if not isinstance(raw_plan, list):
        return []

    checklist: list[ChecklistItem] = []
    for item in raw_plan:
        if not isinstance(item, dict):
            continue
        step = item.get('step')
        status = item.get('status')
        if not isinstance(step, str) or not step.strip():
            continue
        if not isinstance(status, str) or not status.strip():
            status = 'pending'
        checklist.append(ChecklistItem(step=step.strip(), status=status.strip()))
    return checklist


def _extract_codex_commentary_text(data: dict) -> Optional[str]:
    """Extracts visible Codex commentary text that can own follow-up commands."""
    payload = data.get('payload', {})
    if data.get('type') != 'response_item':
        return None
    if payload.get('type') != 'message' or payload.get('role') != 'assistant':
        return None

    phase = payload.get('phase')
    if phase not in {None, 'commentary'}:
        return None

    texts = [
        item.get('text', '').strip()
        for item in payload.get('content', [])
        if isinstance(item, dict)
        and item.get('type') in {'output_text', 'text'}
        and item.get('text', '').strip()
    ]
    if not texts:
        return None
    return '\n\n'.join(texts)


def _extract_codex_reasoning_summary(data: dict) -> Optional[str]:
    """Extracts the latest visible reasoning summary from a Codex event."""
    payload = data.get('payload', {})
    if data.get('type') == 'event_msg' and payload.get('type') == 'agent_reasoning':
        text = payload.get('text', '').strip()
        return text or None

    if data.get('type') != 'response_item' or payload.get('type') != 'reasoning':
        return None

    summary = payload.get('summary', [])
    if not isinstance(summary, list):
        return None

    texts = [
        item.get('text', '').strip()
        for item in summary
        if isinstance(item, dict)
        and item.get('type') == 'summary_text'
        and item.get('text', '').strip()
    ]
    if not texts:
        return None
    return '\n\n'.join(texts)


def _build_checklist_node(checklist: list[ChecklistItem]) -> Optional[SessionTaskNode]:
    """Builds a checklist node with one child per plan step."""
    if not checklist:
        return None

    checklist_node = SessionTaskNode(kind='checklist', prompt='Checklist')
    for item in checklist:
        checklist_node.children.append(
            SessionTaskNode(
                kind='checklist_item',
                prompt=item.step,
                status=item.status,
            )
        )
    return checklist_node


def _extract_claude_reasoning_summary(content: object) -> Optional[str]:
    """Extracts visible reasoning text from a Claude assistant message when available."""
    if not isinstance(content, list):
        return None

    texts = [
        item.get('thinking', '').strip()
        for item in content
        if isinstance(item, dict)
        and item.get('type') == 'thinking'
        and item.get('thinking', '').strip()
    ]
    if not texts:
        return None
    return '\n\n'.join(texts)


def _export_opencode_session(session_id: str) -> dict:
    """Exports an opencode session via the CLI into a temp file."""
    fd, tmp_path = tempfile.mkstemp(suffix='.json', prefix='opencode_export_')
    os.close(fd)

    cmd = [_opencode_bin(), "export", session_id]
    try:
        with open(tmp_path, 'w') as outfile:
            subprocess.run(
                cmd, check=True, stdout=outfile, stderr=subprocess.PIPE, text=True,
            )
    except FileNotFoundError as exc:
        os.unlink(tmp_path)
        raise RuntimeError(
            f"opencode binary not found at {_opencode_bin()!r}. "
            "Install opencode or set OPENCODE_BIN."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or "").strip()
        os.unlink(tmp_path)
        if details:
            raise RuntimeError(f"opencode export failed: {details}") from exc
        raise RuntimeError(f"opencode export for {session_id} failed") from exc

    try:
        with open(tmp_path, 'r') as infile:
            return json.load(infile)
    finally:
        os.unlink(tmp_path)


def _extract_opencode_subagent_id(tool_output: str) -> Optional[str]:
    """Extracts a subagent session ID from a task tool's output."""
    match = re.search(r'task_id:\s*(ses_\w+)', tool_output)
    return match.group(1) if match else None


def _extract_opencode_checklist_items(tool_input: dict) -> list[ChecklistItem]:
    """Extracts checklist items from a todowrite tool's input."""
    todos = tool_input.get('todos', [])
    if not isinstance(todos, list):
        return []

    checklist: list[ChecklistItem] = []
    for item in todos:
        if not isinstance(item, dict):
            continue
        content = item.get('content')
        status = item.get('status', 'pending')
        if isinstance(content, str) and content.strip() and isinstance(status, str) and status.strip():
            checklist.append(ChecklistItem(step=content.strip(), status=status.strip()))
    return checklist


def _synthesize_opencode_read_command(
    tool_input: dict,
    repo_root_cache: dict[str, Optional[str]],
    *,
    cwd: Optional[str] = None,
) -> Optional[str]:
    """Converts an opencode Read tool call into a shell-like file read command."""
    file_path = tool_input.get('filePath')
    if not isinstance(file_path, str) or not file_path:
        return None

    offset = tool_input.get('offset', 1)
    limit = tool_input.get('limit', 200)
    if not isinstance(offset, int) or offset < 1:
        offset = 1
    if not isinstance(limit, int) or limit < 1:
        limit = 200

    end_line = offset + limit - 1
    normalized_path = _normalize_output_path(file_path, repo_root_cache, cwd=cwd)
    return f"sed -n '{offset},{end_line}p' {shlex.quote(normalized_path)}"


def _synthesize_opencode_grep_command(
    tool_input: dict,
    repo_root_cache: dict[str, Optional[str]],
    *,
    cwd: Optional[str] = None,
) -> Optional[str]:
    """Converts an opencode Grep tool call into a shell-like grep command."""
    pattern = tool_input.get('pattern')
    path = tool_input.get('path')
    if not isinstance(pattern, str) or not pattern or not isinstance(path, str) or not path:
        return None

    parts = ['rg', '-n']
    include = tool_input.get('include')
    if isinstance(include, str) and include:
        parts.extend(['-g', include])

    parts.extend([pattern, _normalize_output_path(path, repo_root_cache, cwd=cwd)])
    return ' '.join(shlex.quote(part) for part in parts)


def _parse_opencode_session_file_step(
    session_data: dict,
) -> list[SessionTaskNode]:
    """Parses an opencode session export into structured request nodes."""
    messages = session_data.get('messages', [])
    default_kind = 'user'

    request_markers: list[tuple[int, str]] = []
    for idx, msg in enumerate(messages):
        if msg.get('info', {}).get('role') != 'user':
            continue
        texts = [
            part['text'].strip()
            for part in msg.get('parts', [])
            if part.get('type') == 'text' and part.get('text', '').strip()
        ]
        if not texts:
            continue
        text = '\n'.join(texts)
        if request_markers and request_markers[-1][1] == text and idx - request_markers[-1][0] <= 2:
            continue
        request_markers.append((idx, text))

    if not request_markers:
        request_markers.append((-1, ''))

    parsed_requests: list[SessionTaskNode] = []
    repo_root_cache: dict[str, Optional[str]] = {}

    for marker_idx, (request_idx, request_text) in enumerate(request_markers):
        next_request_idx = (
            request_markers[marker_idx + 1][0]
            if marker_idx + 1 < len(request_markers)
            else len(messages)
        )
        request_prompt = request_text.strip()

        request_node = SessionTaskNode(kind=default_kind, prompt=request_prompt)
        last_output_node = request_node

        for msg in messages[request_idx + 1:next_request_idx]:
            msg_info = msg.get('info', {})
            parts = msg.get('parts', [])
            path_info = msg_info.get('path')
            current_cwd = (
                path_info.get('cwd').strip()
                if isinstance(path_info, dict) and isinstance(path_info.get('cwd'), str) and path_info['cwd'].strip()
                else None
            )

            for part in parts:
                part_type = part.get('type')

                if part_type == 'text':
                    text = part.get('text', '').strip()
                    if text:
                        commentary_node = _append_or_reuse_child(
                            request_node,
                            kind='commentary',
                            prompt=text,
                        )
                        last_output_node = commentary_node

                elif part_type == 'reasoning':
                    text = part.get('text', '').strip()
                    if text:
                        _append_or_reuse_child(
                            request_node,
                            kind='thinking',
                            prompt=text,
                        )

                elif part_type == 'tool':
                    tool_name = part.get('tool')
                    tool_state = part.get('state', {})
                    if tool_state.get('status') == 'error':
                        continue
                    tool_input = tool_state.get('input', {})

                    if tool_name == 'bash':
                        command = tool_input.get('command')
                        if isinstance(command, str) and command:
                            _append_command_node(last_output_node, command)

                    elif tool_name == 'read':
                        command = _synthesize_opencode_read_command(
                            tool_input, repo_root_cache, cwd=current_cwd,
                        )
                        if command:
                            _append_command_node(last_output_node, command)

                    elif tool_name == 'grep':
                        command = _synthesize_opencode_grep_command(
                            tool_input, repo_root_cache, cwd=current_cwd,
                        )
                        if command:
                            _append_command_node(last_output_node, command)

                    elif tool_name == 'glob':
                        pattern = tool_input.get('pattern', '')
                        if isinstance(pattern, str) and pattern:
                            _append_command_node(last_output_node, f"glob {pattern}")

                    elif tool_name == 'write':
                        file_path = tool_input.get('filePath', '')
                        if isinstance(file_path, str) and file_path:
                            normalized_path = _normalize_output_path(
                                file_path, repo_root_cache, cwd=current_cwd,
                            )
                            _append_command_node(
                                last_output_node, f"write {shlex.quote(normalized_path)}",
                            )

                    elif tool_name == 'edit':
                        file_path = tool_input.get('filePath', '')
                        if isinstance(file_path, str) and file_path:
                            normalized_path = _normalize_output_path(
                                file_path, repo_root_cache, cwd=current_cwd,
                            )
                            _append_command_node(
                                last_output_node, f"edit {shlex.quote(normalized_path)}",
                            )

                    elif tool_name == 'todowrite':
                        checklist_items = _extract_opencode_checklist_items(tool_input)
                        checklist_node = _build_checklist_node(checklist_items)
                        if checklist_node is not None:
                            request_node.children.append(checklist_node)

                    elif tool_name == 'task':
                        prompt = tool_input.get('prompt', '')
                        description = tool_input.get('description', '')
                        if not isinstance(prompt, str):
                            prompt = ''
                        if not isinstance(description, str):
                            description = ''
                        agent_prompt = description or prompt
                        placeholder = SessionTaskNode(kind='subagent', prompt=agent_prompt)

                        tool_output = tool_state.get('output', '')
                        subagent_id = _extract_opencode_subagent_id(tool_output) if isinstance(tool_output, str) else None
                        if subagent_id:
                            placeholder.subagent_ids.append(subagent_id)

                        request_node.children.append(placeholder)

        parsed_requests.append(request_node)

    return _compact_task_tree(parsed_requests)


def _attach_inline_subagents(nodes: list[SessionTaskNode]) -> list[SessionTaskNode]:
    """Reparents inline child requests into previously spawned placeholders."""
    pending_placeholders: list[SessionTaskNode] = []
    top_level_nodes: list[SessionTaskNode] = []

    for node in nodes:
        match_index: Optional[int] = None
        for index, placeholder in enumerate(pending_placeholders):
            if placeholder.prompt == node.prompt:
                match_index = index
                break

        if match_index is not None:
            node.kind = 'subagent'
            placeholder = pending_placeholders.pop(match_index)
            _merge_session_node(placeholder, node)
            current_node = placeholder
        else:
            top_level_nodes.append(node)
            current_node = node

        for child in current_node.children:
            if child.kind == 'subagent' and child.prompt:
                pending_placeholders.append(child)

    return top_level_nodes


def _group_activity_under_commentary(
    activity_children: list[SessionTaskNode],
) -> list[SessionTaskNode]:
    """Normalizes local activity into commentary branches."""
    commentary_children: list[SessionTaskNode] = []

    for child in activity_children:
        if child.kind == 'commentary':
            commentary_children.append(child)
            continue

        if (
            commentary_children
            and commentary_children[-1].kind == 'commentary'
            and commentary_children[-1].synthetic
            and not commentary_children[-1].prompt
        ):
            commentary_children[-1].children.append(child)
            continue

        commentary_children.append(
            SessionTaskNode(
                kind='commentary',
                prompt='',
                children=[child],
                synthetic=True,
            )
        )

    return commentary_children


def _build_owner_activity_children(node: SessionTaskNode) -> list[SessionTaskNode]:
    """Compacts owner-local events into checklist and thinking branches."""
    subagent_children: list[SessionTaskNode] = []
    local_activity: list[SessionTaskNode] = []
    latest_checklist: Optional[SessionTaskNode] = None
    latest_thinking: Optional[SessionTaskNode] = None

    for child in node.children:
        if child.kind == 'subagent':
            subagent_children.append(child)
        elif child.kind == 'checklist':
            latest_checklist = child
        elif child.kind == 'thinking':
            latest_thinking = child
        else:
            local_activity.append(child)

    commentary_children = _group_activity_under_commentary(local_activity)
    thinking_node = latest_thinking
    if commentary_children or thinking_node is not None:
        if thinking_node is None:
            thinking_node = SessionTaskNode(
                kind='thinking',
                prompt='',
                synthetic=True,
            )
        thinking_node.children.extend(commentary_children)

    compacted_children = list(subagent_children)
    if latest_checklist is not None:
        if thinking_node is not None:
            latest_checklist.children.append(thinking_node)
        compacted_children.append(latest_checklist)
    elif thinking_node is not None:
        compacted_children.append(thinking_node)
    return compacted_children


def _compact_task_tree(nodes: list[SessionTaskNode]) -> list[SessionTaskNode]:
    """Compacts event timelines into a cleaner task/subtask hierarchy."""
    compacted: list[SessionTaskNode] = []

    for node in nodes:
        node.children = _compact_task_tree(node.children)

        if node.kind in {'user', 'subagent'}:
            compacted_children = _build_owner_activity_children(node)
            if node.kind == 'user':
                local_children = [
                    child
                    for child in compacted_children
                    if child.kind != 'subagent'
                ]
                subagent_children = [
                    child
                    for child in compacted_children
                    if child.kind == 'subagent'
                ]
                node.children = list(subagent_children)
                if local_children:
                    node.children.insert(
                        0,
                        SessionTaskNode(
                            kind='subagent',
                            prompt='main',
                            children=local_children,
                            synthetic=True,
                        ),
                    )
            else:
                node.children = compacted_children

        compacted.append(node)

    return compacted


def _parse_codex_session_file_step(
    session_data: list[dict],
) -> list[SessionTaskNode]:
    """Parses a Codex session file into structured request nodes."""
    is_subagent_session = _is_subagent_session(session_data)
    default_kind = 'subagent' if is_subagent_session else 'user'

    request_markers: list[tuple[int, str]] = []
    for idx, data in enumerate(session_data):
        request = _extract_request_text(data)
        if request is None:
            continue
        if request_markers and request_markers[-1][1] == request and idx - request_markers[-1][0] <= 2:
            continue
        request_markers.append((idx, request))

    if not request_markers:
        request_markers.append((-1, ''))

    parsed_requests: list[SessionTaskNode] = []
    for marker_idx, (request_idx, request_text) in enumerate(request_markers):
        next_request_idx = (
            request_markers[marker_idx + 1][0]
            if marker_idx + 1 < len(request_markers)
            else len(session_data)
        )
        request_prompt = request_text.strip()

        request_node = SessionTaskNode(kind=default_kind, prompt=request_prompt)
        last_output_node = request_node
        spawn_agent_placeholders: dict[str, SessionTaskNode] = {}
        unassigned_spawn_placeholders: list[SessionTaskNode] = []

        for data in session_data[request_idx + 1:next_request_idx]:
            payload = data.get('payload', {})
            commentary_text = _extract_codex_commentary_text(data)
            if commentary_text:
                commentary_node = _append_or_reuse_child(
                    request_node,
                    kind='commentary',
                    prompt=commentary_text,
                )
                last_output_node = commentary_node

            reasoning_summary = _extract_codex_reasoning_summary(data)
            if reasoning_summary:
                _append_or_reuse_child(
                    request_node,
                    kind='thinking',
                    prompt=reasoning_summary,
                )

            if data.get('type') == 'response_item' and payload.get('type') == 'function_call':
                if payload.get('name') == 'exec_command':
                    try:
                        cmd = json.loads(payload.get('arguments', '{}')).get('cmd')
                    except json.JSONDecodeError:
                        cmd = None
                    if cmd:
                        _append_command_node(last_output_node, cmd)
                elif payload.get('name') == 'spawn_agent':
                    prompt_text = _extract_codex_spawn_agent_prompt(payload) or ''
                    placeholder = SessionTaskNode(kind='subagent', prompt=prompt_text)
                    request_node.children.append(placeholder)
                    unassigned_spawn_placeholders.append(placeholder)
                    call_id = payload.get('call_id')
                    if isinstance(call_id, str) and call_id:
                        spawn_agent_placeholders[call_id] = placeholder
                elif payload.get('name') == 'update_plan':
                    try:
                        plan_payload = json.loads(payload.get('arguments', '{}'))
                    except json.JSONDecodeError:
                        plan_payload = {}
                    checklist_node = _build_checklist_node(
                        _extract_checklist_items(plan_payload.get('plan'))
                    )
                    if checklist_node is not None:
                        request_node.children.append(checklist_node)

            if data.get('type') == 'response_item' and payload.get('type') == 'function_call_output':
                call_id = payload.get('call_id')
                if call_id in spawn_agent_placeholders:
                    placeholder = spawn_agent_placeholders[call_id]
                    try:
                        agent_id = json.loads(payload.get('output', '{}')).get('agent_id')
                    except json.JSONDecodeError:
                        agent_id = None
                    if agent_id and agent_id not in placeholder.subagent_ids:
                        placeholder.subagent_ids.append(agent_id)

            notification_agent_id = _extract_subagent_notification_id(data)
            if notification_agent_id:
                for placeholder in unassigned_spawn_placeholders:
                    if not placeholder.subagent_ids:
                        placeholder.subagent_ids.append(notification_agent_id)
                        break

        parsed_requests.append(request_node)

    return _compact_task_tree(_attach_inline_subagents(parsed_requests))


def _extract_claude_message_text(content: object) -> Optional[str]:
    """Returns text content from a Claude message payload when present."""
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if not isinstance(content, list):
        return None

    texts = [
        item.get('text', '').strip()
        for item in content
        if isinstance(item, dict)
        and item.get('type') == 'text'
        and item.get('text', '').strip()
    ]
    if not texts:
        return None
    return '\n'.join(texts)


def _extract_claude_request_text(data: dict) -> Optional[str]:
    """Extracts a real user request from a Claude Code event."""
    if data.get('type') != 'user' or data.get('isMeta'):
        return None

    message = data.get('message', {})
    if message.get('role') != 'user':
        return None

    text = _extract_claude_message_text(message.get('content'))
    if not text:
        return None
    if text.startswith('<local-command-') or text.startswith('<command-name>'):
        return None
    return text


def _claude_message_and_collector_key(data: dict) -> tuple[Optional[dict], Optional[str]]:
    """Returns the logical Claude message payload and its command collector key."""
    if data.get('type') in {'assistant', 'user'}:
        message = data.get('message')
        return (message if isinstance(message, dict) else None, 'main')

    if data.get('type') != 'progress' or data.get('data', {}).get('type') != 'agent_progress':
        return None, None

    progress_message = data.get('data', {}).get('message', {})
    if not isinstance(progress_message, dict):
        return None, None

    message = progress_message.get('message')
    if not isinstance(message, dict):
        return None, None

    collector_key = data.get('data', {}).get('agentId')
    return message, collector_key if isinstance(collector_key, str) else None


def _iter_claude_tool_uses(data: dict) -> list[tuple[str, dict]]:
    """Returns Claude tool_use entries paired with their target collector key."""
    message, collector_key = _claude_message_and_collector_key(data)
    if collector_key is None or message is None or message.get('role') != 'assistant':
        return []

    content = message.get('content', [])
    if not isinstance(content, list):
        return []

    return [
        (collector_key, item)
        for item in content
        if isinstance(item, dict) and item.get('type') == 'tool_use'
    ]


def _iter_claude_tool_results(data: dict) -> list[tuple[str, dict, dict]]:
    """Returns Claude tool_result entries paired with their collector key and metadata."""
    message, collector_key = _claude_message_and_collector_key(data)
    if collector_key is None or message is None or message.get('role') != 'user':
        return []

    content = message.get('content', [])
    if not isinstance(content, list):
        return []

    tool_result_metadata = data.get('toolUseResult', {})
    if not isinstance(tool_result_metadata, dict):
        tool_result_metadata = {}

    return [
        (collector_key, item, tool_result_metadata)
        for item in content
        if isinstance(item, dict) and item.get('type') == 'tool_result'
    ]


def _normalize_output_path(
    path: str,
    repo_root_cache: dict[str, Optional[str]],
    *,
    cwd: Optional[str] = None,
) -> str:
    """Normalizes a path for display, preferring repo-relative or cwd-relative forms."""
    normalized_path = os.path.normpath(path)
    if not os.path.isabs(normalized_path):
        return _display_path(normalized_path)

    repo_root = _git_repo_root_for_path(normalized_path, repo_root_cache)
    if repo_root is not None:
        return _display_path(os.path.relpath(normalized_path, repo_root))

    if cwd:
        normalized_cwd = os.path.abspath(cwd)
        try:
            if os.path.commonpath([normalized_path, normalized_cwd]) == normalized_cwd:
                return _display_path(os.path.relpath(normalized_path, normalized_cwd))
        except ValueError:
            pass

    return _display_path(normalized_path)


def _synthesize_claude_read_command(
    tool_input: dict,
    repo_root_cache: dict[str, Optional[str]],
    tool_result_metadata: Optional[dict] = None,
    *,
    cwd: Optional[str] = None,
) -> Optional[str]:
    """Converts a Claude Read tool call into a shell-like file read command."""
    metadata = tool_result_metadata or {}
    file_metadata = metadata.get('file', {})
    if not isinstance(file_metadata, dict):
        file_metadata = {}

    file_path = tool_input.get('file_path') or file_metadata.get('filePath')
    if not isinstance(file_path, str) or not file_path:
        return None

    start_line = file_metadata.get('startLine')
    if not isinstance(start_line, int) or start_line < 1:
        offset = tool_input.get('offset')
        start_line = offset if isinstance(offset, int) and offset > 0 else 1

    num_lines = file_metadata.get('numLines')
    if not isinstance(num_lines, int) or num_lines < 1:
        limit = tool_input.get('limit')
        if isinstance(limit, int) and limit > 0:
            num_lines = limit
        else:
            total_lines = file_metadata.get('totalLines')
            if isinstance(total_lines, int) and total_lines >= start_line:
                num_lines = total_lines - start_line + 1
            else:
                num_lines = 200

    end_line = start_line + num_lines - 1
    normalized_path = _normalize_output_path(
        file_path,
        repo_root_cache,
        cwd=cwd,
    )
    return f"sed -n '{start_line},{end_line}p' {shlex.quote(normalized_path)}"


def _synthesize_claude_grep_command(
    tool_input: dict,
    repo_root_cache: dict[str, Optional[str]],
    *,
    cwd: Optional[str] = None,
) -> Optional[str]:
    """Converts a Claude Grep tool call into a shell-like grep command."""
    pattern = tool_input.get('pattern')
    path = tool_input.get('path')
    if not isinstance(pattern, str) or not pattern or not isinstance(path, str) or not path:
        return None

    parts = ['rg', '-n']
    context = tool_input.get('context')
    if isinstance(context, int) and context > 0:
        parts.extend(['-C', str(context)])

    glob = tool_input.get('glob')
    if isinstance(glob, str) and glob:
        parts.extend(['-g', glob])
    elif isinstance(glob, list):
        for pattern_glob in glob:
            if isinstance(pattern_glob, str) and pattern_glob:
                parts.extend(['-g', pattern_glob])

    parts.extend(
        [
            pattern,
            _normalize_output_path(path, repo_root_cache, cwd=cwd),
        ]
    )
    return ' '.join(shlex.quote(part) for part in parts)


def _parse_claude_session_file_step(
    session_data: list[dict]
) -> list[SessionTaskNode]:
    """Parses a Claude Code session file into structured request nodes."""
    request_markers: list[tuple[int, str]] = []
    for idx, data in enumerate(session_data):
        request = _extract_claude_request_text(data)
        if request is None:
            continue
        if request_markers and request_markers[-1][1] == request and idx - request_markers[-1][0] <= 2:
            continue
        request_markers.append((idx, request))

    if not request_markers:
        request_markers.append((-1, ''))

    parsed_requests: list[SessionTaskNode] = []
    for marker_idx, (request_idx, request_text) in enumerate(request_markers):
        next_request_idx = (
            request_markers[marker_idx + 1][0]
            if marker_idx + 1 < len(request_markers)
            else len(session_data)
        )
        request_prompt = request_text.strip()

        request_node = SessionTaskNode(kind='user', prompt=request_prompt)
        collector_nodes: dict[str, SessionTaskNode] = {'main': request_node}
        last_output_by_collector: dict[str, SessionTaskNode] = {'main': request_node}
        agent_prompts_by_tool_id: dict[str, str] = {}
        pending_reads: dict[str, tuple[str, dict]] = {}
        repo_root_cache: dict[str, Optional[str]] = {}
        
        def collector_node(collector_key: str) -> SessionTaskNode:
            return collector_nodes.get(collector_key, request_node)

        def command_parent(collector_key: str) -> SessionTaskNode:
            return last_output_by_collector.get(collector_key, collector_node(collector_key))

        for data in session_data[request_idx + 1:next_request_idx]:
            cwd = data.get('cwd')
            current_cwd = cwd.strip() if isinstance(cwd, str) and cwd.strip() else None

            if data.get('type') == 'progress' and data.get('data', {}).get('type') == 'agent_progress':
                agent_id = data.get('data', {}).get('agentId')
                parent_tool_use_id = data.get('parentToolUseID')
                if isinstance(agent_id, str) and agent_id:
                    agent_prompt = ''
                    if isinstance(parent_tool_use_id, str):
                        agent_prompt = agent_prompts_by_tool_id.get(parent_tool_use_id, '')
                    if not agent_prompt:
                        prompt_data = data.get('data', {}).get('prompt')
                        if isinstance(prompt_data, str):
                            agent_prompt = prompt_data.strip()

                    node = collector_nodes.get(agent_id)
                    if node is None:
                        node = SessionTaskNode(kind='subagent', prompt=agent_prompt)
                        request_node.children.append(node)
                        collector_nodes[agent_id] = node
                        last_output_by_collector[agent_id] = node
                    elif agent_prompt and not node.prompt:
                        node.prompt = agent_prompt

            message, collector_key = _claude_message_and_collector_key(data)
            if message is not None and collector_key is not None:
                owner_node = collector_node(collector_key)
                commentary_text = None
                if message.get('role') == 'assistant':
                    commentary_text = _extract_claude_message_text(message.get('content'))
                if commentary_text:
                    commentary_node = _append_or_reuse_child(
                        owner_node,
                        kind='commentary',
                        prompt=commentary_text,
                    )
                    last_output_by_collector[collector_key] = commentary_node

                reasoning_summary = _extract_claude_reasoning_summary(message.get('content'))
                if reasoning_summary:
                    _append_or_reuse_child(
                        owner_node,
                        kind='thinking',
                        prompt=reasoning_summary,
                    )

            for collector_key, tool_use in _iter_claude_tool_uses(data):
                tool_name = tool_use.get('name')
                tool_id = tool_use.get('id')
                tool_input = tool_use.get('input', {})
                if not isinstance(tool_input, dict):
                    tool_input = {}

                if tool_name == 'Bash':
                    command = tool_input.get('command')
                    if isinstance(command, str) and command:
                        _append_command_node(command_parent(collector_key), command)
                elif tool_name == 'Read' and isinstance(tool_id, str) and tool_id:
                    pending_reads[tool_id] = (collector_key, tool_input)
                elif tool_name == 'Grep':
                    command = _synthesize_claude_grep_command(
                        tool_input,
                        repo_root_cache,
                        cwd=current_cwd,
                    )
                    if command:
                        _append_command_node(command_parent(collector_key), command)
                elif tool_name == 'Agent' and isinstance(tool_id, str) and tool_id:
                    agent_prompt = tool_input.get('prompt')
                    if isinstance(agent_prompt, str):
                        agent_prompts_by_tool_id[tool_id] = agent_prompt.strip()

            for _, tool_result, tool_result_metadata in _iter_claude_tool_results(data):
                tool_use_id = tool_result.get('tool_use_id')
                if not isinstance(tool_use_id, str) or tool_use_id not in pending_reads:
                    continue
                collector_key, tool_input = pending_reads.pop(tool_use_id)
                command = _synthesize_claude_read_command(
                    tool_input,
                    repo_root_cache,
                    tool_result_metadata,
                    cwd=current_cwd,
                )
                if command:
                    _append_command_node(command_parent(collector_key), command)

        for collector_key, tool_input in pending_reads.values():
            command = _synthesize_claude_read_command(tool_input, repo_root_cache)
            if command:
                _append_command_node(command_parent(collector_key), command)

        parsed_requests.append(request_node)

    return _compact_task_tree(parsed_requests)


def parse_session_file_step(session_file_path: str) -> list[SessionTaskNode]:
    """Parses a session file into structured task nodes."""
    if session_file_path.startswith('opencode://'):
        session_id = session_file_path[len('opencode://'):]
        session_data = _export_opencode_session(session_id)
        return _parse_opencode_session_file_step(session_data)

    session_data = _load_session_data(session_file_path)
    session_format = _detect_session_format(session_data)
    if session_format == 'codex':
        return _parse_codex_session_file_step(session_data)
    return _parse_claude_session_file_step(session_data)


def session_id_to_session_file(session_id: str) -> Optional[str]:
    """Given a session ID, returns the path to the session file."""
    if session_id.startswith('ses_'):
        return f"opencode://{session_id}"

    session_roots = [
        os.path.expanduser('~/.codex/sessions'),
        os.path.expanduser('~/.claude/projects'),
    ]
    for sessions_root in session_roots:
        if not os.path.isdir(sessions_root):
            continue
        for root, _, files in os.walk(sessions_root):
            for file in files:
                if file.endswith('.jsonl') and session_id in file:
                    return os.path.join(root, file)
    return None


def parse_session_file(session_file_path: str) -> list[SessionTaskNode]:
    """Parses a session file and its subagents into a hierarchical task tree."""
    visited_files: set[str] = set()

    def resolve_subagents(node: SessionTaskNode) -> None:
        for child in node.children:
            resolve_subagents(child)

        for session_id in list(node.subagent_ids):
            session_file = session_id_to_session_file(session_id)
            if session_file is None:
                continue

            child_nodes = load_task_nodes(session_file)
            if len(child_nodes) == 1 and child_nodes[0].kind == 'subagent':
                _merge_session_node(node, child_nodes[0])
            else:
                node.children.extend(child_nodes)

        node.subagent_ids = []

    def load_task_nodes(path: str) -> list[SessionTaskNode]:
        if path in visited_files:
            return []
        visited_files.add(path)

        nodes = parse_session_file_step(path)
        for node in nodes:
            resolve_subagents(node)
        return nodes

    return load_task_nodes(session_file_path)


_OPENCODE_BIN: Optional[str] = None


def _opencode_bin() -> str:
    """Returns the path to the opencode binary."""
    global _OPENCODE_BIN
    if _OPENCODE_BIN is None:
        _OPENCODE_BIN = os.environ.get('OPENCODE_BIN', 'opencode')
    return _OPENCODE_BIN


_OPENCODE_DB_PATH: Optional[str] = None


def _opencode_db_path() -> str:
    """Returns the path to the opencode SQLite database."""
    global _OPENCODE_DB_PATH
    if _OPENCODE_DB_PATH is None:
        _OPENCODE_DB_PATH = os.environ.get(
            'OPENCODE_DB_PATH',
            os.path.expanduser('~/.local/share/opencode/opencode.db'),
        )
    return _OPENCODE_DB_PATH


def _build_coverage_prompt(validation_error: Optional[str] = None) -> str:
    """Builds the prompt used to update coverage.json."""
    if validation_error is None:
        return coverage_prompt

    return (
        f"{coverage_prompt}\n"
        "The previous attempt was invalid.\n"
        f"Validation error: {validation_error}\n"
        "Fix coverage.json in place and satisfy every requirement above. "
        "Do not drop, reorder, or rewrite commands."
    )


def _run_opencode_coverage_update(tmpdirname: str, validation_error: Optional[str] = None) -> None:
    """Asks opencode to update coverage.json in the temporary directory."""
    opencode_cmd = [
        _opencode_bin(),
        "run",
        _build_coverage_prompt(validation_error),
        "--format",
        "json",
        "--agent",
        "plan",
        "--dir",
        tmpdirname,
        "--dangerously-skip-permissions",
    ]

    try:
        subprocess.run(
            opencode_cmd,
            check=True,
            cwd=tmpdirname,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"opencode binary not found at {_opencode_bin()!r}. "
            "Install opencode or set OPENCODE_BIN."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        if details:
            raise RuntimeError(f"opencode run failed: {details}") from exc
        raise RuntimeError("opencode run failed without stderr output") from exc


def _format_json_decode_error(exc: json.JSONDecodeError) -> str:
    """Formats a JSON decoding error for prompt feedback."""
    return (
        "coverage.json is not valid JSON: "
        f"{exc.msg} at line {exc.lineno} column {exc.colno} (char {exc.pos})."
    )


def _validate_range_spec(range_spec: object, command_index: int, range_index: int) -> None:
    """Validates a single path:start:end range spec."""
    prefix = f"commands[{command_index}].ranges[{range_index}]"
    if not isinstance(range_spec, str):
        raise ValueError(f"{prefix} must be a string in path:start:end format.")

    parts = range_spec.rsplit(":", 2)
    if len(parts) != 3:
        raise ValueError(f"{prefix} must use path:start:end format, got {range_spec!r}.")

    path, start_text, end_text = parts
    if not path:
        raise ValueError(f"{prefix} must include a non-empty path, got {range_spec!r}.")

    try:
        start = int(start_text)
        end = int(end_text)
    except ValueError as exc:
        raise ValueError(
            f"{prefix} must end with integer line numbers, got {range_spec!r}."
        ) from exc

    if start < 1 or end < 1:
        raise ValueError(
            f"{prefix} must use positive line numbers, got {range_spec!r}."
        )
    if start > end:
        raise ValueError(
            f"{prefix} start line {start} must be <= end line {end}."
        )


def _validate_coverage_data(
    updated_coverage: object, commands: list[str]
) -> list[dict[str, list[str]]]:
    """Validates the generated coverage JSON and returns the typed payload."""
    if not isinstance(updated_coverage, list):
        raise ValueError("coverage.json must contain a JSON array.")
    if len(updated_coverage) != len(commands):
        raise ValueError(
            "coverage.json must contain exactly "
            f"{len(commands)} command entries, found {len(updated_coverage)}."
        )

    for idx, expected_cmd in enumerate(commands):
        prefix = f"commands[{idx}]"
        item = updated_coverage[idx]
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} must be an object.")

        cmd = item.get("cmd")
        if cmd != expected_cmd:
            raise ValueError(
                f"{prefix}.cmd must match the input command exactly. "
                f"Expected {expected_cmd!r}, got {cmd!r}."
            )

        ranges = item.get("ranges")
        if not isinstance(ranges, list):
            raise ValueError(f"{prefix}.ranges must be an array.")
        for range_index, range_spec in enumerate(ranges):
            _validate_range_spec(range_spec, idx, range_index)

    return updated_coverage


def _display_path(path: str) -> str:
    """Formats a path for JSON output using forward slashes."""
    return path.replace(os.sep, '/')


def _find_existing_parent(path: str) -> Optional[str]:
    """Returns the nearest existing parent directory for a path, if any."""
    candidate = os.path.abspath(path)
    while candidate and not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return None
        candidate = parent
    return candidate if os.path.exists(candidate) else None


def _git_repo_root_for_path(
    path: str, repo_root_cache: dict[str, Optional[str]]
) -> Optional[str]:
    """Returns the git repo root containing the path, if one can be resolved."""
    existing_parent = _find_existing_parent(path)
    if existing_parent is None:
        return None

    search_dir = existing_parent if os.path.isdir(existing_parent) else os.path.dirname(existing_parent)
    if search_dir in repo_root_cache:
        return repo_root_cache[search_dir]

    try:
        git_root = subprocess.run(
            ['git', '-C', search_dir, 'rev-parse', '--show-toplevel'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        git_root = ''

    repo_root = os.path.abspath(git_root) if git_root else None
    repo_root_cache[search_dir] = repo_root
    return repo_root


def _normalize_range_spec_path(
    range_spec: str, repo_root_cache: dict[str, Optional[str]]
) -> str:
    """Normalizes a range spec path to repo-relative form when possible."""
    path, start_text, end_text = range_spec.rsplit(':', 2)
    if not os.path.isabs(path):
        return f"{_display_path(os.path.normpath(path))}:{start_text}:{end_text}"

    repo_root = _git_repo_root_for_path(path, repo_root_cache)
    if repo_root is None:
        return f"{_display_path(os.path.normpath(path))}:{start_text}:{end_text}"

    relative_path = os.path.relpath(path, repo_root)
    return f"{_display_path(relative_path)}:{start_text}:{end_text}"


def _normalize_coverage_data(
    coverage_data: list[dict[str, list[str]]]
) -> list[dict[str, list[str]]]:
    """Normalizes coverage ranges for stable JSON output."""
    repo_root_cache: dict[str, Optional[str]] = {}
    return [
        {
            'cmd': entry['cmd'],
            'ranges': [
                _normalize_range_spec_path(range_spec, repo_root_cache)
                for range_spec in entry['ranges']
            ],
        }
        for entry in coverage_data
    ]


def _load_and_validate_coverage_file(
    coverage_file_path: str, commands: list[str]
) -> list[dict[str, list[str]]]:
    """Loads and validates coverage.json."""
    with open(coverage_file_path, 'r') as f:
        raw_coverage = f.read()

    try:
        updated_coverage = json.loads(raw_coverage)
    except json.JSONDecodeError as exc:
        raise ValueError(_format_json_decode_error(exc)) from exc

    return _normalize_coverage_data(_validate_coverage_data(updated_coverage, commands))


def _commands_chunk_to_coverage(commands: list[str]) -> list[dict[str, list[str]]]:
    """
    Given a bounded list of commands, returns the generated coverage entries.

    Limitation of this approach:
    for f in triaged/H-*.md triaged/TH-*.md triaged/M-*.md triaged/TM-*.md; do [ -e "$f" ] && echo "=== $f ===" && sed -n '1,120p' "$f"; done
    """
    # wrap commands in a json array of the form:
    # [{"cmd: "cli command 1", "ranges: ["file_path:line_start:line_end", ...]},
    # {"cmd: "cli command 2", "ranges: ["file_path:line_start:line_end", ...]},
    #  ...]
    if not commands:
        return []

    prepared_coverage: list[Optional[dict[str, list[str]]]] = [None] * len(commands)
    unresolved_commands: list[str] = []
    unresolved_indexes: list[int] = []

    for index, cmd in enumerate(commands):
        parsed_ranges = _parse_command_ranges(cmd)
        if parsed_ranges is None:
            unresolved_indexes.append(index)
            unresolved_commands.append(cmd)
            continue
        prepared_coverage[index] = {'cmd': cmd, 'ranges': parsed_ranges}

    if not unresolved_commands:
        return _normalize_coverage_data([
            entry for entry in prepared_coverage if entry is not None
        ])

    coverage_data = [{"cmd": cmd, "ranges": []} for cmd in unresolved_commands]
    json_coverage_data = json.dumps(coverage_data)
    with tempfile.TemporaryDirectory() as tmpdirname:
        coverage_file_path = os.path.join(tmpdirname, 'coverage.json')
        with open(coverage_file_path, 'w') as f:
            f.write(json_coverage_data)

        if os.name == "posix":
            os.chmod(coverage_file_path, 0o600)
            os.chmod(tmpdirname, 0o500)

        validation_error: Optional[str] = None
        for attempt in range(MAX_COVERAGE_FIX_ATTEMPTS + 1):
            _run_opencode_coverage_update(tmpdirname, validation_error)
            try:
                generated_coverage = _load_and_validate_coverage_file(
                    coverage_file_path, unresolved_commands
                )
                for index, entry in zip(unresolved_indexes, generated_coverage):
                    prepared_coverage[index] = entry
                return _normalize_coverage_data([
                    entry for entry in prepared_coverage if entry is not None
                ])
            except ValueError as exc:
                validation_error = str(exc)
                if attempt == MAX_COVERAGE_FIX_ATTEMPTS:
                    raise RuntimeError(
                        "Failed to produce a valid coverage.json after "
                        f"{MAX_COVERAGE_FIX_ATTEMPTS + 1} attempts. "
                        f"Last validation error: {validation_error}"
                    ) from exc

    raise AssertionError("unreachable")


def commands_to_coverage(commands: list[str]) -> list[dict[str, list[str]]]:
    """Generates coverage entries for a command list, chunking large batches."""
    if not commands:
        return []

    coverage: list[dict[str, list[str]]] = []
    for chunk_start in range(0, len(commands), MAX_COMMANDS_PER_COVERAGE_CHUNK):
        chunk = commands[chunk_start:chunk_start + MAX_COMMANDS_PER_COVERAGE_CHUNK]
        coverage.extend(_commands_chunk_to_coverage(chunk))

    return coverage


def _walk_task_nodes(task_nodes: list[SessionTaskNode]) -> list[SessionTaskNode]:
    """Returns a flattened pre-order traversal of the task tree."""
    flattened: list[SessionTaskNode] = []
    for node in task_nodes:
        flattened.append(node)
        flattened.extend(_walk_task_nodes(node.children))
    return flattened


def task_tree_to_coverage(task_nodes: list[SessionTaskNode]) -> dict[str, object]:
    """Generates hierarchical coverage while reusing repeated command results."""
    if not task_nodes:
        return {'format': 'hierarchical-v1', 'tasks': []}

    flattened_nodes = _walk_task_nodes(task_nodes)
    unique_commands: list[str] = []
    for node in flattened_nodes:
        for cmd in node.commands:
            if cmd not in unique_commands:
                unique_commands.append(cmd)

    unique_coverage = commands_to_coverage(unique_commands)
    ranges_by_command = {
        entry["cmd"]: list(entry["ranges"])
        for entry in unique_coverage
    }

    def node_to_coverage(node: SessionTaskNode) -> dict[str, object]:
        serialized_node: dict[str, object] = {
            'kind': node.kind,
            'prompt': node.prompt,
            'coverage': [
                {'cmd': cmd, 'ranges': list(ranges_by_command.get(cmd, []))}
                for cmd in node.commands
            ],
        }
        if node.status:
            serialized_node['status'] = node.status
        if node.synthetic:
            serialized_node['synthetic'] = True
        if node.children:
            serialized_node['children'] = [node_to_coverage(child) for child in node.children]
        return serialized_node

    return {
        'format': 'hierarchical-v1',
        'tasks': [node_to_coverage(node) for node in task_nodes],
    }

def run(args: argparse.Namespace) -> dict[str, object]:
    task_nodes: list[SessionTaskNode] = []
    if args.session_file:
        task_nodes = parse_session_file(args.session_file)

    if args.session_id:
        session_file = session_id_to_session_file(args.session_id)
        if session_file is None:
            return {'format': 'hierarchical-v1', 'tasks': []}
        task_nodes = parse_session_file(session_file)

    coverage = task_tree_to_coverage(task_nodes)

    if args.output_file:
        with open(args.output_file, 'w') as f:
            json.dump(coverage, f, indent=2)
    else:
        print(json.dumps(coverage, indent=2))

    return coverage

def main() -> None:
    parser = argparse.ArgumentParser(description='Parse session to coverage')

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--session-file', type=str,
                       help='Path to the session file')
    group.add_argument('--otter-agent-file', type=str,
                       help='Path to the otter agent file')
    group.add_argument('--session-id', type=str,
                       help='Session ID of the session to parse')

    parser.add_argument('--output-file', type=str, default='coverage_by_request.json',
                       help='Path to the output coverage file')

    args = parser.parse_args()
    run(args)
    
if __name__ == '__main__':
    main()
