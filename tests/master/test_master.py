# The piwheels project
#   Copyright (c) 2017 Ben Nuttall <https://github.com/bennuttall>
#   Copyright (c) 2017 Dave Jones <dave@waveform.org.uk>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the copyright holder nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


import os
from unittest import mock
from threading import Thread

import zmq
import pytest

from conftest import find_message
from piwheels import __version__
from piwheels.master import main, const


@pytest.fixture()
def mock_pypi(request):
    with mock.patch('xmlrpc.client.ServerProxy') as proxy:
        proxy().changelog_since_serial.return_value = []
        yield proxy


@pytest.fixture()
def mock_signal(request):
    with mock.patch('signal.signal') as signal:
        yield signal


@pytest.fixture()
def mock_context(request, zmq_context):
    with mock.patch('zmq.Context.instance') as ctx_mock:
        # Pass thru calls to Context.socket, but ignore everything else (in
        # particular, destroy and term calls as we want the testing context to
        # stick around)
        ctx_mock().socket.side_effect = zmq_context.socket
        yield ctx_mock


@pytest.fixture()
def master_thread(request, mock_pypi, mock_context, mock_systemd, mock_signal,
                  tmpdir, db_url, db, with_schema):
    main_thread = Thread(daemon=True, target=main, args=([
        '--dsn', db_url,
        '--output-path', str(tmpdir.join('output')),
        '--status-queue',  'ipc://' + str(tmpdir.join('status-queue')),
        '--control-queue', 'ipc://' + str(tmpdir.join('control-queue')),
        '--slave-queue',   'ipc://' + str(tmpdir.join('slave-queue')),
        '--file-queue',    'ipc://' + str(tmpdir.join('file-queue')),
        '--import-queue',  'ipc://' + str(tmpdir.join('import-queue')),
        '--log-queue',     'ipc://' + str(tmpdir.join('log-queue')),
    ],))
    yield main_thread
    if main_thread.is_alive():
        with mock_context().socket(zmq.PUSH) as control:
            control.connect('ipc://' + str(tmpdir.join('control-queue')))
            control.send_pyobj(['QUIT'])
        main_thread.join(10)
        assert not main_thread.is_alive()


@pytest.fixture()
def master_control(request, tmpdir, mock_context):
    control = mock_context().socket(zmq.PUSH)
    control.connect('ipc://' + str(tmpdir.join('control-queue')))
    yield control
    control.close()


def test_help(capsys):
    with pytest.raises(SystemExit):
        main(['--help'])
    out, err = capsys.readouterr()
    assert out.startswith('usage:')
    assert '--pypi-xmlrpc' in out


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(['--version'])
    out, err = capsys.readouterr()
    assert out.strip() == __version__


def test_no_root(caplog):
    with mock.patch('os.geteuid') as geteuid:
        geteuid.return_value = 0
        assert main([]) != 0
        assert find_message(caplog.records, 'Master must not be run as root')


def test_quit_control(mock_systemd, master_thread, master_control):
    master_thread.start()
    assert mock_systemd._ready.wait(10)
    master_control.send_pyobj(['QUIT'])
    master_thread.join(10)
    assert not master_thread.is_alive()


def test_system_exit(mock_systemd, master_thread, caplog):
    with mock.patch('piwheels.master.PiWheelsMaster.main_loop') as main_loop:
        main_loop.side_effect = SystemExit(1)
        master_thread.start()
        assert mock_systemd._ready.wait(10)
        master_thread.join(10)
        assert not master_thread.is_alive()
    assert find_message(caplog.records, 'shutting down on SIGTERM')


def test_system_ctrl_c(mock_systemd, master_thread, caplog):
    with mock.patch('piwheels.master.PiWheelsMaster.main_loop') as main_loop:
        main_loop.side_effect = KeyboardInterrupt()
        master_thread.start()
        assert mock_systemd._ready.wait(10)
        master_thread.join(10)
        assert not master_thread.is_alive()
    assert find_message(caplog.records, 'shutting down on Ctrl+C')


def test_bad_control(mock_systemd, master_thread, master_control, caplog):
    master_thread.start()
    assert mock_systemd._ready.wait(10)
    master_control.send_pyobj(['FOO'])
    master_control.send_pyobj(['QUIT'])
    master_thread.join(10)
    assert not master_thread.is_alive()
    assert find_message(caplog.records, 'ignoring invalid FOO message')


def test_status_passthru(tmpdir, mock_context, mock_systemd, master_thread):
    master_thread.start()
    assert mock_systemd._ready.wait(10)
    with mock_context().socket(zmq.PUSH) as int_status, \
            mock_context().socket(zmq.SUB) as ext_status:
        ext_status.connect('ipc://' + str(tmpdir.join('status-queue')))
        ext_status.setsockopt_string(zmq.SUBSCRIBE, '')
        # Wait for the first statistics message (from BigBrother) to get the
        # SUB queue working
        ext_status.recv_pyobj()
        int_status.connect(const.INT_STATUS_QUEUE)
        int_status.send_pyobj(['FOO'])
        # Try several times to read the passed-thru message; other messages
        # (like stats from BigBrother) will be sent to ext-status too
        for i in range(3):
            if ext_status.recv_pyobj() == ['FOO']:
                break
        else:
            assert False, "Didn't see FOO passed-thru the status pipe"


def test_kill_control(mock_systemd, master_thread, master_control):
    with mock.patch('piwheels.master.SlaveDriver.kill_slave') as kill_slave:
        master_thread.start()
        assert mock_systemd._ready.wait(10)
        master_control.send_pyobj(['KILL', 1])
        master_control.send_pyobj(['QUIT'])
        master_thread.join(10)
        assert not master_thread.is_alive()
        assert kill_slave.call_args == mock.call(1)


def test_pause_resume(mock_systemd, master_thread, master_control, caplog):
    master_thread.start()
    assert mock_systemd._ready.wait(10)
    master_control.send_pyobj(['PAUSE'])
    master_control.send_pyobj(['RESUME'])
    master_control.send_pyobj(['QUIT'])
    master_thread.join(10)
    assert not master_thread.is_alive()
    assert find_message(caplog.records, 'pausing operations')
    assert find_message(caplog.records, 'resuming operations')


def test_new_monitor(mock_systemd, master_thread, master_control, caplog):
    with mock.patch('piwheels.master.SlaveDriver.list_slaves') as list_slaves:
        master_thread.start()
        assert mock_systemd._ready.wait(10)
        master_control.send_pyobj(['HELLO'])
        master_control.send_pyobj(['QUIT'])
        master_thread.join(10)
        assert not master_thread.is_alive()
        assert find_message(caplog.records, 'sending status to new monitor')
        assert list_slaves.call_args == mock.call()
