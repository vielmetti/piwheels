#!/usr/bin/env python

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

"""
The piw-sense application is a fun version of the monitor that uses a Raspberry
Pi Sense HAT to provide a "physical" interface for monitoring and controlling
the master node.
"""

import sys
import signal
from collections import OrderedDict
from threading import Thread, main_thread
from time import sleep

import zmq
from pisense import SenseHAT, array
from colorzero import Color

from .. import terminal, const
from .tasks import Task, TaskQuit
from .renderers import MainRenderer, StatusRenderer, QuitRenderer


class PiWheelsSense:
    """
    This is the main class for the :program:`piw-sense` script. It
    connects to the :program:`piw-master` script via the control and external
    status queues, and displays the real-time status of the master on the
    attached Raspberry Pi Sense HAT.
    """

    def __init__(self):
        self.hat = None
        self.status_queue = None
        self.ctrl_queue = None

    def __call__(self, args=None):
        parser = terminal.configure_parser(__doc__, log_params=False)
        parser.add_argument(
            '--status-queue', metavar='ADDR', default=const.STATUS_QUEUE,
            help="The address of the queue used to report status to monitors "
            "(default: %(default)s)")
        parser.add_argument(
            '--control-queue', metavar='ADDR', default=const.CONTROL_QUEUE,
            help="The address of the queue a monitor can use to control the "
            "master (default: %(default)s)")
        parser.add_argument(
            '-r', '--rotate', metavar='DEGREES', default=0, type=int,
            help="The rotation of the HAT in degrees; must be 0 (the default) "
            "90, 180, or 270")
        try:
            config = parser.parse_args(args)
        except:  # pylint: disable=bare-except
            return terminal.error_handler(*sys.exc_info())

        with SenseHAT() as hat:
            hat.rotation = config.rotate
            ctx = zmq.Context()
            try:
                stick = StickTask(config, hat)
                stick.start()
                screen = ScreenTask(config, hat)
                screen.start()
                signal.sigwait({signal.SIGINT, signal.SIGTERM})
            except KeyboardInterrupt:
                pass
            finally:
                screen.quit()
                screen.join()
                stick.quit()
                stick.join()
                ctx.destroy(linger=1000)
                ctx.term()
                hat.screen.fade_to(array(Color('black')))


class StickTask(Thread):
    def __init__(self, config, hat):
        super().__init__()
        self._quit = False
        self.stick = hat.stick
        self.ctx = zmq.Context.instance()
        self.stick_queue = self.ctx.socket(zmq.PUSH)
        self.stick_queue.hwm = 10
        self.stick_queue.bind('inproc://stick')

    def quit(self):
        self._quit = True

    def run(self):
        try:
            while not self._quit:
                event = self.stick.read(0.1)
                if event is not None and event.pressed:
                    self.stick_queue.send_pyobj(event)
        finally:
            self.stick_queue.close()


class ScreenTask(Task):
    name = "screen"

    def __init__(self, config, hat):
        super().__init__()
        self.screen = hat.screen
        self.renderers = {}
        self.renderers['main'] = MainRenderer()
        self.renderers['quit'] = QuitRenderer(self.renderers['main'])
        self.renderers['status'] = StatusRenderer(self.renderers['main'])
        self._renderer = None
        self._screen_iter = None
        self.renderer = self.renderers['main']
        self.transition = self.screen.fade_to
        stick_queue = self.ctx.socket(zmq.PULL)
        stick_queue.hwm = 10
        stick_queue.connect('inproc://stick')
        status_queue = self.ctx.socket(zmq.SUB)
        status_queue.hwm = 10
        status_queue.connect(config.status_queue)
        status_queue.setsockopt_string(zmq.SUBSCRIBE, '')
        self.register(stick_queue, self.handle_stick)
        self.register(status_queue, self.handle_status)
        sleep(1)
        self.ctrl_queue = self.ctx.socket(zmq.PUSH)
        self.ctrl_queue.connect(config.control_queue)
        self.ctrl_queue.send_pyobj(['HELLO'])

    def poll(self):
        super().poll(1 / 15)

    def loop(self):
        self.transition(next(self._screen_iter))
        self.transition = self.screen.draw

    @property
    def renderer(self):
        return self._renderer

    @renderer.setter
    def renderer(self, value):
        self._renderer = value
        self._screen_iter = iter(value)

    def handle_stick(self, queue):
        event = queue.recv_pyobj()
        self.renderer.move(event, self)

    def handle_status(self, queue):
        """
        Handler for messages received from the PUB/SUB external status queue.
        As usual, messages are a list of python objects. In this case messages
        always have at least 3 elements:

        * The slave id that the message relates to (this will be -1 in the case
          of messages that don't relate to a specific build slave)
        * The timestamp when the message was sent
        * The message itself
        """
        self.renderers['main'].message(*queue.recv_pyobj())


main = PiWheelsSense()  # pylint: disable=invalid-name

if __name__ == '__main__':
    main()
