#!/usr/bin/env python
# License: GPLv3 Copyright: 2021, Kovid Goyal <kovid at kovidgoyal.net>

import os
import posixpath
from collections import deque
from contextlib import suppress
from enum import auto
from itertools import count
from time import monotonic
from typing import Deque, Dict, Iterator, List, Optional

from kitty.cli_stub import TransferCLIOptions
from kitty.fast_data_types import FILE_TRANSFER_CODE
from kitty.file_transmission import (
    Action, Compression, FileTransmissionCommand, FileType, NameReprEnum,
    encode_bypass
)
from kitty.typing import KeyEventType
from kitty.utils import sanitize_control_codes

from ..tui.handler import Handler
from ..tui.loop import Loop, debug
from ..tui.operations import styled, without_line_wrap
from ..tui.utils import human_size
from .send import Transfer
from .utils import expand_home, random_id, should_be_compressed

debug
file_counter = count(1)


class State(NameReprEnum):
    waiting_for_permission = auto()
    waiting_for_file_metadata = auto()
    transferring = auto()
    canceled = auto()


class File:

    def __init__(self, ftc: FileTransmissionCommand):
        self.expected_size = ftc.size
        self.transmit_started_at = self.done_at = 0.
        self.transmitted_bytes = 0
        self.ftype = ftc.ftype
        self.mtime = ftc.mtime
        self.spec_id = int(ftc.file_id)
        self.permissions = ftc.permissions
        self.remote_path = ftc.name
        self.display_name = sanitize_control_codes(self.remote_path)
        self.remote_id = ftc.status
        self.remote_target = ftc.data.decode('utf-8')
        self.parent = ftc.parent
        self.expanded_local_path = ''
        self.file_id = str(next(file_counter))
        self.compression_capable = self.ftype is FileType.regular and self.expected_size > 4096 and should_be_compressed(self.expanded_local_path)
        self.remote_symlink_value = b''
        self.local_write_started = False

    def __repr__(self) -> str:
        return f'File(rpath={self.remote_path!r}, lpath={self.expanded_local_path!r})'

    def write_data(self, data: bytes, is_last: bool) -> int:
        if self.ftype is FileType.symlink:
            self.remote_symlink_value += data
            return 0
        if self.ftype is FileType.regular:
            if not self.local_write_started:
                parent = os.path.dirname(self.expanded_local_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                self.local_write_started = True
            with open(self.expanded_local_path, 'ab') as f:
                base = f.tell()
                f.write(data)
                return f.tell() - base
        return 0

    def apply_metadata(self) -> None:
        if self.ftype is FileType.symlink:
            with suppress(NotImplementedError):
                os.chmod(self.expanded_local_path, self.permissions, follow_symlinks=False)
                os.utime(self.expanded_local_path, ns=(self.mtime, self.mtime), follow_symlinks=False)
        else:
            os.chmod(self.expanded_local_path, self.permissions)
            os.utime(self.expanded_local_path, ns=(self.mtime, self.mtime))


class TreeNode:

    def __init__(self, file: File, local_name: str, parent: Optional['TreeNode'] = None):
        self.entry = file
        self.entry.expanded_local_path = local_name
        self.parent = parent
        self.added_files: Dict[int, TreeNode] = {}

    def add_child(self, file: File) -> 'TreeNode':
        q = self.added_files.get(id(file))
        if q is not None:
            return q
        c = TreeNode(file, os.path.join(self.entry.expanded_local_path, os.path.basename(file.remote_path)), self)
        self.added_files[id(file)] = c
        return c

    def __iter__(self) -> Iterator['TreeNode']:
        for c in self.added_files.values():
            yield c
            yield from c


def make_tree(all_files: List[File], local_base: str) -> TreeNode:
    fid_map = {f.remote_id: f for f in all_files}
    node_map: Dict[str, TreeNode] = {}
    root_node = TreeNode(File(FileTransmissionCommand(file_id='-1')), local_base)

    def ensure_parent(f: File) -> TreeNode:
        if not f.parent:
            return root_node
        parent = node_map.get(f.parent)
        if parent is None:
            fp = fid_map[f.parent]
            gp = ensure_parent(fp)
            parent = gp.add_child(fp)
        return parent

    for f in all_files:
        p = ensure_parent(f)
        p.add_child(f)
    return root_node


def files_for_receive(cli_opts: TransferCLIOptions, dest: str, files: List[File], remote_home: str, specs: List[str]) -> Iterator[File]:
    spec_map: Dict[int, List[File]] = {i: [] for i in range(len(specs))}
    for f in files:
        spec_map[f.spec_id].append(f)
    spec_paths = [spec_map[i][0].remote_path for i in range(len(specs))]
    if cli_opts.mode == 'mirror':
        try:
            common_path = posixpath.commonpath(spec_paths)
        except ValueError:
            common_path = ''
        home = remote_home.rstrip('/')
        if common_path and common_path.startswith(home + '/'):
            spec_paths = [posixpath.join('~', posixpath.relpath(x, home)) for x in spec_paths]
        for spec_id, files_for_spec in spec_map.items():
            spec = spec_paths[spec_id]
            tree = make_tree(files_for_spec, os.path.dirname(expand_home(spec)))
            for x in tree:
                yield x.entry
    else:
        dest_is_dir = dest[-1] in (os.sep, os.altsep) or len(specs) > 1
        for spec_id, files_for_spec in spec_map.items():
            if dest_is_dir:
                dest_path = os.path.join(dest, posixpath.basename(files_for_spec[0].remote_path))
            else:
                dest_path = dest
            tree = make_tree(files_for_spec, os.path.dirname(expand_home(dest_path)))
            for x in tree:
                yield x.entry


class ProgressTracker:

    def __init__(self) -> None:
        self.total_size_of_all_files = 0
        self.total_bytes_to_transfer = 0
        self.active_file: Optional[File] = None
        self.total_transferred = 0
        self.transfers: Deque[Transfer] = deque()
        self.transfered_stats_amt = 0
        self.transfered_stats_interval = 0.
        self.started_at = 0.
        self.signature_bytes = 0

    def change_active_file(self, nf: File) -> None:
        now = monotonic()
        self.active_file = nf
        nf.transmit_started_at = now

    def start_transfer(self) -> None:
        self.transfers.append(Transfer())
        self.started_at = monotonic()

    def file_written(self, af: File, amt: int, is_done: bool) -> None:
        self.active_file = af
        af.transmitted_bytes += amt
        self.total_transferred += amt
        self.transfers.append(Transfer(amt))
        now = self.transfers[-1].at
        while len(self.transfers) > 2 and self.transfers[0].is_too_old(now):
            self.transfers.popleft()
        self.transfered_stats_interval = now - self.transfers[0].at
        self.transfered_stats_amt = sum(t.amt for t in self.transfers)
        if is_done:
            af.done_at = monotonic()


class Manager:

    def __init__(
        self, request_id: str, spec: List[str], dest: str,
        bypass: Optional[str] = None
    ):
        self.request_id = request_id
        self.spec = spec
        self.failed_specs: Dict[int, str] = {}
        self.spec_counts = dict.fromkeys(range(len(self.spec)), 0)
        self.dest = dest
        self.remote_home = ''
        self.bypass = encode_bypass(request_id, bypass) if bypass else ''
        self.prefix = f'\x1b]{FILE_TRANSFER_CODE};id={self.request_id};'
        self.suffix = '\x1b\\'
        self.state = State.waiting_for_permission
        self.files: List[File] = []
        self.progress_tracker = ProgressTracker()
        self.transfer_done = False

    def start_transfer(self) -> Iterator[str]:
        yield FileTransmissionCommand(action=Action.receive, bypass=self.bypass, size=len(self.spec)).serialize()
        for i, x in enumerate(self.spec):
            yield FileTransmissionCommand(action=Action.file, file_id=str(i), name=x).serialize()
        self.progress_tracker.start_transfer()

    def finalize_transfer(self) -> str:
        self.transfer_done = True
        rid_map = {f.remote_id: f for f in self.files}
        for f in self.files:
            if f.ftype is FileType.directory:
                try:
                    os.makedirs(f.expanded_local_path, exist_ok=True)
                except OSError as err:
                    return str(err)
            elif f.ftype is FileType.link:
                target = rid_map.get(f.remote_target)
                if target is None:
                    return f'Hard link with remote id: {f.remote_target} not found'
                try:
                    os.makedirs(os.path.dirname(f.expanded_local_path), exist_ok=True)
                    os.link(target.expanded_local_path, f.expanded_local_path)
                except OSError as err:
                    return str(err)
            elif f.ftype is FileType.symlink:
                if f.remote_target:
                    target = rid_map.get(f.remote_target)
                    if target is None:
                        return f'Symbolic link with remote id: {f.remote_target} not found'
                    lt = target.expanded_local_path
                    if not f.remote_symlink_value.startswith(b'/'):
                        lt = os.path.relpath(lt, os.path.dirname(f.expanded_local_path))
                else:
                    lt = f.remote_symlink_value.decode('utf-8')
                os.symlink(lt, f.expanded_local_path)
            with suppress(OSError):
                f.apply_metadata()
        return ''

    def request_files(self) -> Iterator[str]:
        for f in self.files:
            if f.ftype is FileType.directory or (f.ftype is FileType.link and f.remote_target):
                continue
            yield FileTransmissionCommand(
                action=Action.file, name=f.remote_path, file_id=f.file_id,
                compression=Compression.zlib if f.compression_capable else Compression.none
            ).serialize()

    def collect_files(self, cli_opts: TransferCLIOptions) -> None:
        self.files = list(files_for_receive(cli_opts, self.dest, self.files, self.remote_home, self.spec))
        self.progress_tracker.total_size_of_all_files = sum(max(0, f.expected_size) for f in self.files)
        self.progress_tracker.total_bytes_to_transfer = self.progress_tracker.total_size_of_all_files
        self.fid_map = {f.file_id: f for f in self.files}

    def on_file_transfer_response(self, ftc: FileTransmissionCommand) -> str:
        if self.state is State.waiting_for_permission:
            if ftc.action is Action.status:
                if ftc.status == 'OK':
                    self.state = State.waiting_for_file_metadata
                else:
                    return 'Permission for transfer denied'
            else:
                return f'Unexpected response from terminal: {ftc}'
        elif self.state is State.waiting_for_file_metadata:
            if ftc.action is Action.status:
                if ftc.file_id:
                    try:
                        fid = int(ftc.file_id)
                    except Exception:
                        return f'Unexpected response from terminal: {ftc}'
                    if fid < 0 or fid >= len(self.spec):
                        return f'Unexpected response from terminal: {ftc}'
                    self.failed_specs[fid] = ftc.status
                else:
                    if ftc.status == 'OK':
                        self.state = State.transferring
                        self.remote_home = ftc.name
                        return ''
                    else:
                        return ftc.status
            elif ftc.action is Action.file:
                try:
                    fid = int(ftc.file_id)
                except Exception:
                    return f'Unexpected response from terminal: {ftc}'
                if fid < 0 or fid >= len(self.spec):
                    return f'Unexpected response from terminal: {ftc}'
                self.spec_counts[fid] += 1
                self.files.append(File(ftc))
            else:
                return f'Unexpected response from terminal: {ftc}'
        elif self.state is State.transferring:
            if ftc.action in (Action.data, Action.end_data):
                f = self.fid_map.get(ftc.file_id)
                if f is None:
                    return f'Got data for unknown file id: {ftc.file_id}'
                is_last = ftc.action is Action.end_data
                try:
                    amt_written = f.write_data(ftc.data, is_last)
                except OSError as err:
                    return str(err)
                self.progress_tracker.file_written(f, amt_written, is_last)
                if is_last:
                    del self.fid_map[ftc.file_id]
                    if not self.fid_map:
                        return self.finalize_transfer()
        return ''


class Receive(Handler):
    use_alternate_screen = False

    def __init__(self, cli_opts: TransferCLIOptions, spec: List[str], dest: str = ''):
        self.cli_opts = cli_opts
        self.manager = Manager(random_id(), spec, dest, bypass=cli_opts.permissions_bypass)
        self.quit_after_write_code: Optional[int] = None
        self.check_paths_printed = False
        self.transmit_started = False

    def send_payload(self, payload: str) -> None:
        self.write(self.manager.prefix)
        self.write(payload)
        self.write(self.manager.suffix)

    def initialize(self) -> None:
        self.cmd.set_cursor_visible(False)
        self.print('Scanning files…')
        for x in self.manager.start_transfer():
            self.send_payload(x)

    def finalize(self) -> None:
        self.cmd.set_cursor_visible(True)

    def on_file_transfer_response(self, ftc: FileTransmissionCommand) -> None:
        if ftc.id != self.manager.request_id:
            return
        if ftc.status == 'CANCELED' and ftc.action is Action.status:
            self.quit_loop(1)
            return
        if self.quit_after_write_code is not None or self.manager.state is State.canceled:
            return
        transfer_started = self.manager.state is State.transferring
        err = self.manager.on_file_transfer_response(ftc)
        if err:
            self.print_err(err)
            self.print('Waiting to ensure terminal cancels transfer, will quit in a few seconds')
            self.abort_transfer()
            return
        if not transfer_started and self.manager.state is State.transferring:
            if self.manager.failed_specs:
                self.print_err('Failed to process some sources')
                for spec_id, msg in self.manager.failed_specs.items():
                    spec = self.manager.spec[spec_id]
                    self.print(f'{spec}: {msg}')
                self.quit_loop(1)
                return
            if 0 in self.manager.spec_counts.values():
                self.print_err('No matches found for: ' + ', '.join(self.manager.spec[k] for k, v in self.manager.spec_counts.items() if v == 0))
                self.quit_loop(1)
                return
            self.manager.collect_files(self.cli_opts)
            if self.cli_opts.confirm_paths:
                self.confirm_paths()
            else:
                self.start_transfer()
        if self.manager.transfer_done:
            self.exit_after_completion()

    def exit_after_completion(self) -> None:
        self.quit_loop(0)

    def confirm_paths(self) -> None:
        self.print_check_paths()

    def print_check_paths(self) -> None:
        if self.check_paths_printed:
            return
        self.check_paths_printed = True
        self.print('The following file transfers will be performed. A red destination means an existing file will be overwritten.')
        for df in self.manager.files:
            self.cmd.styled(df.ftype.short_text, fg=df.ftype.color)
            self.print(end=' ')
            self.print(df.display_name, '→', end=' ')
            self.cmd.styled(df.expanded_local_path, fg='red' if os.path.lexists(df.expanded_local_path) else None)
            self.print()
        self.print(f'Transferring {len(self.manager.files)} file(s) of total size: {human_size(self.manager.progress_tracker.total_size_of_all_files)}')
        self.print()
        self.print_continue_msg()

    def on_text(self, text: str, in_bracketed_paste: bool = False) -> None:
        if self.quit_after_write_code is not None:
            return
        if self.check_paths_printed and not self.transmit_started:
            if text.lower() == 'y':
                self.start_transfer()
                return
            if text.lower() == 'n':
                self.abort_transfer()
                self.print('Sending cancel request to terminal')
                return
            self.print_continue_msg()

    def on_key(self, key_event: KeyEventType) -> None:
        if self.quit_after_write_code is not None:
            return
        if key_event.matches('esc'):
            if self.check_paths_printed and not self.transmit_started:
                self.abort_transfer()
                self.print('Sending cancel request to terminal')
            else:
                self.on_interrupt()

    def print_continue_msg(self) -> None:
        self.print(
            'Press', styled('y', fg='green', bold=True, fg_intense=True), 'to continue or',
            styled('n', fg='red', bold=True, fg_intense=True), 'to abort')

    def start_transfer(self) -> None:
        self.transmit_started = True
        self.print(f'Queueing transfer of {len(self.manager.files)} files(s)')
        for x in self.manager.request_files():
            self.send_payload(x)

    def print_err(self, msg: str) -> None:
        self.cmd.styled(msg, fg='red')
        self.print()

    def on_term(self) -> None:
        if self.quit_after_write_code is not None:
            return
        self.print_err('Terminate requested, cancelling transfer, transferred files are in undefined state')
        self.abort_transfer(delay=2)

    def on_interrupt(self) -> None:
        if self.quit_after_write_code is not None:
            return
        if self.manager.state is State.canceled:
            self.print('Waiting for canceled acknowledgement from terminal, will abort in a few seconds if no response received')
            return
        self.print_err('Interrupt requested, cancelling transfer, transferred files are in undefined state')
        self.abort_transfer()

    def abort_transfer(self, delay: float = 5) -> None:
        self.send_payload(FileTransmissionCommand(action=Action.cancel).serialize())
        self.manager.state = State.canceled
        self.asyncio_loop.call_later(delay, self.quit_loop, 1)

    @Handler.atomic_update
    def draw_progress(self) -> None:
        if self.manager.state is State.canceled:
            return
        with without_line_wrap(self.write):
            pass


def receive_main(cli_opts: TransferCLIOptions, args: List[str]) -> None:
    dest = ''
    if cli_opts.mode == 'mirror':
        if len(args) < 1:
            raise SystemExit('Must specify at least one file to transfer')
        spec = list(args)
    else:
        if len(args) < 2:
            raise SystemExit('Must specify at least one source and a destination file to transfer')
        spec, dest = args[:-1], args[-1]

    loop = Loop()
    handler = Receive(cli_opts, spec, dest)
    loop.loop(handler)
