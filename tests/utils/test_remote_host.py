"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import backoff
import pytest
import re
from flexmock import flexmock, Mock
from functools import wraps
from typing import Callable, Optional, Tuple

from atomic_reactor.utils.rpm import rpm_qf_args


def _do_nothing_decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


# Disable backoff retries in tests before import our module
flexmock(backoff).should_receive("on_exception").and_return(_do_nothing_decorator)


from atomic_reactor.utils.remote_host import (  # noqa
    SSHRetrySession, RemoteHost, RemoteHostsPool
)


SOCKET_PATH = "/run/user/2022/podman/podman.sock"


@pytest.fixture(autouse=True)
def _mock_ssh_session(request):
    """ Mock the ssh session with things we don't want to test or change """
    if "disable_autouse" in request.keywords:
        yield
    else:
        flexmock(SSHRetrySession).should_receive("connect")
        flexmock(RemoteHost).should_receive("slots_dir").and_return("/home/builder/osbs_slots")
        yield


def make_ssh_result(
    stdout: str = "",
    stderr: str = "",
    code: int = 0
) -> Tuple[None, Mock, Mock]:
    """ Produce a fake non-blocking ssh exec_command result """

    chan = flexmock()
    chan.should_receive("recv_exit_status").and_return(code)

    out = flexmock(channel=chan)
    out.should_receive("read.decode.strip").and_return(stdout)

    err = flexmock()
    err.should_receive("read.decode.strip").and_return(stderr)
    return None, out, err


def make_flock_ssh_result(
    stdout: str = "",
    stderr: str = "",
    code: int = 0,
    stdin_write_callback: Optional[Callable] = None
) -> Tuple[Mock, Mock, Mock]:
    """ Produce a fake ssh flock exec_command result """
    # This ssh flock command is blocking in session, stdin need to be mocked
    stdin = flexmock()
    if stdin_write_callback is None:
        stdin.should_receive("write")
    else:
        stdin.should_receive("write").replace_with(stdin_write_callback)
    stdin.should_receive("flush")
    stdin.should_receive("close")

    chan = flexmock()
    chan.should_receive("recv_exit_status").and_return(code)
    out = flexmock(channel=chan)
    out.should_receive("read.decode.strip").and_return(stdout)
    out.should_receive("readline").and_return(stdout)

    err = flexmock()
    err.should_receive("read.decode.strip").and_return(stderr)
    return stdin, out, err


@pytest.mark.parametrize(("mkdir_stderr", "mkdir_code", "expected_result"), (
    ("", 0, True),
    ("mkdir: cannot create directory: ... permission denied", 1, False),
))
def test_host_is_operational(mkdir_stderr, mkdir_code, expected_result, caplog):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)

    def mocked_command(cmd, *args, **kwargs):
        if cmd == "mkdir -p /home/builder/osbs_slots":
            return make_ssh_result(stderr=mkdir_stderr, code=mkdir_code)

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )

    operational = host.is_operational
    assert operational is expected_result

    if not operational:
        assert mkdir_stderr in caplog.text


@pytest.mark.parametrize(("rpm_stderr", "rpm_code", "expected_result"), (
    ("", 0, 'list;of;rpms'),
    ("rpm: no rpm db found", 1, None),
))
def test_rpms_installed(rpm_stderr, rpm_code, expected_result, caplog):
    host_name = "remote-host-001"
    host = RemoteHost(hostname=host_name, username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)

    def mocked_command(cmd, *args, **kwargs):
        if not expected_result:
            raise Exception(rpm_stderr)

        if cmd == f"rpm {rpm_qf_args()}":
            return make_ssh_result(stdout=expected_result, stderr=rpm_stderr, code=rpm_code)

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )

    rpms = host.rpms_installed

    assert rpms == expected_result
    if not expected_result:
        msg = f"can't get rpms from host: {host_name} : {rpm_stderr}"
        assert msg in caplog.text


@pytest.mark.disable_autouse
def test_using_non_default_slots_dir():
    slots_dir = "/var/tmp/osbs/slots/"
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH,
                      slots_dir=slots_dir)

    flexmock(SSHRetrySession).should_receive("connect")

    def mocked_command(cmd, *args, **kwargs):
        if cmd == f"mkdir -p {slots_dir}":
            return make_ssh_result()

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )
    assert host.is_operational


def test_check_slot_is_free_with_invalid_id(caplog):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)
    # slot id starts from 0
    with host._ssh_session() as session:
        free = host.is_free(3, session)
    assert "remote-host-001: invalid slot id 3, should be in" in caplog.text
    assert not free


@pytest.mark.parametrize(("cat_stdout", "cat_stderr", "cat_code", "expected_result"), (
    ("", "", 0, True),
    ("invalid_is_free", "", 0, True),
    ("pr123@2022-02-15T10:22:33.234234", "", 0, False),
))
def test_check_slot_is_free(cat_stdout, cat_stderr, cat_code, expected_result):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)

    def mocked_command(cmd, *args, **kwargs):
        if cmd == "touch /home/builder/osbs_slots/slot_2 && cat /home/builder/osbs_slots/slot_2":
            return make_ssh_result(cat_stdout, cat_stderr, cat_code)

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )
    with host._ssh_session() as session:
        free = host.is_free(2, session)
    assert free is expected_result


def test_lock_a_free_slot(caplog):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)

    def mocked_command(cmd, *args, **kwargs):
        if cmd == "touch /home/builder/osbs_slots/slot_2 && cat /home/builder/osbs_slots/slot_2":
            return make_ssh_result()

        if cmd == ("flock --conflict-exit-code 42 --nonblocking "
                   "/home/builder/osbs_slots/slot_2.lock cat"):
            return make_flock_ssh_result(stdout="verify lock")

        write_patt = re.compile(r"echo pr123@.*> /home/builder/osbs_slots/slot_2")
        if write_patt.match(cmd):
            return make_ssh_result()

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )
    locked = host.lock(2, "pr123")
    assert locked
    assert "remote-host-001: slot 2 is locked for pipelinerun pr123" in caplog.text


def test_lock_an_occupied_slot(caplog):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)

    def mocked_command(cmd, *args, **kwargs):
        if cmd == "touch /home/builder/osbs_slots/slot_2 && cat /home/builder/osbs_slots/slot_2":
            return make_ssh_result(stdout="123@2022-02-15T10:12:13.780426")

        if cmd == ("flock --conflict-exit-code 42 --nonblocking "
                   "/home/builder/osbs_slots/slot_2.lock cat"):
            return make_flock_ssh_result(stdout="")

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )
    locked = host.lock(2, "pr234")
    assert not locked
    assert "remote-host-001: failed to lock slot 2 for pipelinerun pr234" in caplog.text


def test_lock_slot_with_other_locking_on_it(caplog):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)

    def mocked_command(cmd, *args, **kwargs):
        if cmd == "touch /home/builder/osbs_slots/slot_2 && cat /home/builder/osbs_slots/slot_2":
            return make_ssh_result()

        if cmd == ("flock --conflict-exit-code 42 --nonblocking "
                   "/home/builder/osbs_slots/slot_2.lock cat"):
            return make_flock_ssh_result(code=42)

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )
    locked = host.lock(2, "pr123")
    assert not locked
    assert "failed to acquire lock on slot 2: slot is locked by others" in caplog.text


def test_lock_slot_with_flock_cat_error(caplog):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)

    def mocked_command(cmd, *args, **kwargs):
        if cmd == "touch /home/builder/osbs_slots/slot_2 && cat /home/builder/osbs_slots/slot_2":
            return make_ssh_result()

        if cmd == ("flock --conflict-exit-code 42 --nonblocking "
                   "/home/builder/osbs_slots/slot_2.lock cat"):
            return make_flock_ssh_result(
                code=66,
                stdin_write_callback=lambda x: (_ for _ in ()).throw(OSError("socket is closed"))
            )

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )
    locked = host.lock(2, "pr123")
    assert not locked
    assert "failed to acquire lock on slot 2: socket is closed" in caplog.text


def test_lock_an_invalid_slot(caplog):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)
    # Need to return different content for the same read slot commands,
    # which is not easy in a single mocked_command, so set it one by one
    read_slot = "touch /home/builder/osbs_slots/slot_2 && cat /home/builder/osbs_slots/slot_2"
    cmd_kwargs = {"timeout": int}
    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .with_args(read_slot, **cmd_kwargs)
        .and_return(make_ssh_result())  # return empty slot for the first call
        .and_return(make_ssh_result(stdout="invalid_slot_content"))  # return invalid content
        .and_return(make_ssh_result(stdout="pr123@2022-09-22T13:11:23.512100"))
    )
    write_patt = re.compile(r"echo pr123@.*> /home/builder/osbs_slots/slot_2")
    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .with_args(write_patt, **cmd_kwargs)
        .and_return(make_ssh_result())
    )
    flock = "flock --conflict-exit-code 42 --nonblocking /home/builder/osbs_slots/slot_2.lock cat"
    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .with_args(flock)
        .and_return(make_flock_ssh_result(stdout="verify lock"))
    )

    locked = host.lock(2, "pr123")
    assert locked
    assert "remote-host-001: slot 2 is locked for pipelinerun pr123" in caplog.text


@pytest.mark.parametrize(("slot_content", "expected_log", "expected_result"), (
    ("pr123@2022-02-15T10:22:33.234234", "slot 2 is unlocked for pipelinerun pr123", True),
    ("", "slot 2 is free, skip unlocking", True),
    ("invalid_to_unlock", "slot 2 contains invalid content, it's corrupted, will unlock it", True),
    ("pr124@2022-02-15T10:22:33.234234", "failed to unlock slot 2 for pipelinerun pr123", False),
))
def test_unlock_host_slot(slot_content, expected_log, expected_result, caplog):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)

    def mocked_command(cmd, *args, **kwargs):
        if cmd == "touch /home/builder/osbs_slots/slot_2 && cat /home/builder/osbs_slots/slot_2":
            return make_ssh_result(stdout=slot_content)

        if cmd == ("flock --conflict-exit-code 42 --nonblocking "
                   "/home/builder/osbs_slots/slot_2.lock cat"):
            return make_flock_ssh_result(stdout="verify lock")

        write_patt = re.compile(r"truncate -s 0 /home/builder/osbs_slots/slot_2")
        if write_patt.match(cmd):
            return make_ssh_result()

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )
    unlocked = host.unlock(2, "pr123")
    assert unlocked is expected_result
    assert expected_log in caplog.text


@pytest.mark.disable_autouse
@pytest.mark.parametrize(("slot_content", "expected_log", "expected_result", "failure"), (
    ("", "is locked for pipelinerun pr123", True, None),
    ("pr124@2022-02-15T10:22:33.234234",
     "no remote host slot available for pipelinerun pr123", False, None),
    (None, "no remote host slot available for pipelinerun pr123", False, 'slot'),
    ("", "Cannot find remote host resource for pipelinerun pr123",
     False, 'lock'),
))
def test_pool_lock_resource(slot_content, expected_log, expected_result, failure, caplog):
    hosts_config = {
        "slots_dir": "/var/tmp/osbs_slots",
        "pools": {
            "x86_64": {
                "remote-host-001": {
                    "enabled": True,
                    "auth": "/path/to/key",
                    "username": "builder",
                    "slots": 3,
                    "socket_path": SOCKET_PATH,
                }
            }
        }
    }

    if failure == 'slot':
        (flexmock(RemoteHost)
         .should_receive('available_slots')
         .and_raise(Exception))
    elif failure == 'lock':
        (flexmock(RemoteHost)
         .should_receive('lock')
         .and_raise(Exception))

    def mocked_command(cmd, *args, **kwargs):
        if cmd == "mkdir -p /var/tmp/osbs_slots":
            return make_ssh_result()

        read_patt = re.compile(
            r"touch /var/tmp/osbs_slots/slot_.* && cat /var/tmp/osbs_slots/slot_.*"
        )
        if read_patt.match(cmd):
            return make_ssh_result(stdout=slot_content)

        flock_patt = re.compile(
            r"flock --conflict-exit-code 42 --nonblocking /var/tmp/osbs_slots/slot_.*.lock cat"
        )
        if flock_patt.match(cmd):
            return make_flock_ssh_result(stdout="verify lock")

        write_patt = re.compile(r"echo pr123@.*> /var/tmp/osbs_slots/slot_.*")
        if write_patt.match(cmd):
            return make_ssh_result()

        assert False, f"Unexpected command: {cmd}"

    flexmock(SSHRetrySession).should_receive("connect")

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )

    pool = RemoteHostsPool.from_config(hosts_config, platform="x86_64")
    locked = pool.lock_resource("pr123")
    assert bool(locked) is expected_result
    assert expected_log in caplog.text
    if failure == 'slot':
        assert 'unable to get available slots:' in caplog.text
    elif failure == 'lock':
        assert 'remote-host-001: unable to lock slot 0 for pipelinerun pr123:' in caplog.text
        assert 'remote-host-001: unable to lock slot 1 for pipelinerun pr123:' in caplog.text
        assert 'remote-host-001: unable to lock slot 2 for pipelinerun pr123:' in caplog.text


@pytest.mark.parametrize(("slot0", "slot1", "slot2", "available", "occupied"), (
    ("", "", "", {0, 1, 2}, set()),
    ("pr123@2022-02-15T10:22:33.234234", "", "", {1, 2}, {0}),
    ("pr123@2022-02-15T10:22:33.234234", "pr124@2022-02-15T10:22:33.234234", "", {2}, {0, 1}),
    ("pr123@2022-02-15T10:22:33.234234", "pr124@2022-02-15T10:22:33.234234",
     "pr124@2022-02-15T10:22:33.234234", set(), {0, 1, 2}),
))
def test_available_and_occupied_slots(caplog, slot0, slot1, slot2, available, occupied):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)

    def mocked_command(cmd, *args, **kwargs):
        if cmd == "touch /home/builder/osbs_slots/slot_0 && cat /home/builder/osbs_slots/slot_0":
            return make_ssh_result(stdout=slot0)

        if cmd == "touch /home/builder/osbs_slots/slot_1 && cat /home/builder/osbs_slots/slot_1":
            return make_ssh_result(stdout=slot1)

        if cmd == "touch /home/builder/osbs_slots/slot_2 && cat /home/builder/osbs_slots/slot_2":
            return make_ssh_result(stdout=slot2)

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )

    assert set(host.available_slots()) == available
    assert host.occupied_slots() == occupied


@pytest.mark.parametrize(("slot0", "slot1", "slot2", "prid0", "prid1", "prid2"), (
    ("", "", "", None, None, None),
    ("pr123@2022-02-15T10:22:33.234234", "", "", "pr123", None, None),
    ("pr123@2022-02-15T10:22:33.234234", "pr124@2022-02-15T10:22:33.234234", "",
     "pr123", "pr124", None),
    ("pr123@2022-02-15T10:22:33.234234", "pr124@2022-02-15T10:22:33.234234",
     "pr125@2022-02-15T10:22:33.234234", "pr123", "pr124", "pr125"),
))
def test_prid_in_slot(caplog, slot0, slot1, slot2, prid0, prid1, prid2):
    host = RemoteHost(hostname="remote-host-001", username="builder",
                      ssh_keyfile="/path/to/key", slots=3, socket_path=SOCKET_PATH)

    def mocked_command(cmd, *args, **kwargs):
        if cmd == "touch /home/builder/osbs_slots/slot_0 && cat /home/builder/osbs_slots/slot_0":
            return make_ssh_result(stdout=slot0)

        if cmd == "touch /home/builder/osbs_slots/slot_1 && cat /home/builder/osbs_slots/slot_1":
            return make_ssh_result(stdout=slot1)

        if cmd == "touch /home/builder/osbs_slots/slot_2 && cat /home/builder/osbs_slots/slot_2":
            return make_ssh_result(stdout=slot2)

        assert False, f"Unexpected command: {cmd}"

    (
        flexmock(SSHRetrySession)
        .should_receive("exec_command")
        .replace_with(mocked_command)
    )

    assert host.prid_in_slot(0) == prid0
    assert host.prid_in_slot(1) == prid1
    assert host.prid_in_slot(2) == prid2
