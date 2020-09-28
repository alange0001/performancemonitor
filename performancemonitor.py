#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@author: Adriano Lange <alange0001@gmail.com>

Copyright (c) 2020-present, Adriano Lange.  All rights reserved.
This source code is licensed under both the GPLv2 (found in the
LICENSE file in the root directory) and Apache 2.0 License
(found in the LICENSE.Apache file in the root directory).
"""

import config
import util

import time
import asyncio
import threading
import argparse
import signal
import sys

#=============================================================================
import logging
from systemd.journal import JournalHandler
log = logging.getLogger('performancemonitor')
log.addHandler(JournalHandler())
log.setLevel(logging.INFO)

#=============================================================================
class Program:
	_stop = False
	_threads = None

	args = None

	def __init__(self):
		self._threads = []
		self.parse_args()

	def parse_args(self):
		parser = argparse.ArgumentParser(
			description="Performance monitor server.")
		parser.add_argument('-p', '--port', type=int,
			default=config.get_default_port(),
			help='device')
		parser.add_argument('-l', '--log_level', type=str,
			default='INFO', choices=[ 'DEBUG', 'INFO' ],
			help='log level')
		self.args = parser.parse_args()

		log.setLevel(getattr(logging, self.args.log_level))
		log.info('Parameters: {}'.format(str(self.args)))

	def main(self):
		try:
			log.info("Program initiated")
			signal.signal(signal.SIGTERM, lambda signumber, stack: self.signal_handler('SIGTERM', signumber, stack) )
			signal.signal(signal.SIGINT,  lambda signumber, stack: self.signal_handler('SIGINT',  signumber, stack) )

			self.startThread( CmdServer(self) )

			while not self._stop:
				time.sleep(0.5)

		except Exception as e:
			log.critical("exception received: {}".format(str(e)))
			self.stop()
			return 1

		self.stop()
		return 0

	def startThread(self, thread):
		thread.start()
		self._threads.append(thread)

	def stop(self):
		if self._stop: return
		self._stop = True
		for t in self._threads:
			t.stop()
		for t in self._threads:
			t.join()

	def signal_handler(self, signame, signumber, *args):
		log.warning("signal {} received".format(signame))
		self.stop()

#=============================================================================
class CmdServer (threading.Thread):
	_program = None
	_stop_thread = False

	def __init__(self, program):
		self._program = program
		threading.Thread.__init__(self)
		self.name = 'CmdServer'

	def run(self):
		asyncio.run( self.main() )

	def stop(self):
		log.debug('stopping thread CmdServer')
		self._stop_thread = True

	async def main(self):
		task = asyncio.create_task( self.server() )
		while not self._stop_thread:
			await asyncio.sleep(0.3)
		task.cancel()
		log.debug('thread CmdServer stopped')

	async def server(self):
		self._loop = asyncio.get_event_loop()

		handler = lambda reader, writer: self.handler(reader, writer)
		self._server = await asyncio.start_server(
			handler, '127.0.0.1', self._program.args.port )

		addr = self._server.sockets[0].getsockname()
		log.info(f'Serving on {addr}')

		async with self._server:
			await self._server.serve_forever()

	async def handler(self, reader, writer):
		log.debug("Starting handler...")
		data = await reader.read(255)
		message = data.decode()
		addr = writer.get_extra_info('peername')

		log.info(f"Received {message!r} from {addr!r}")

		log.debug(f"Send: {message!r}")
		writer.write(data)
		await writer.drain()

		log.debug("Close the connection")
		writer.close()

#=============================================================================
if __name__ == '__main__':
	r = Program().main()
	log.info('exit {}'.format(r))
	exit(r)
