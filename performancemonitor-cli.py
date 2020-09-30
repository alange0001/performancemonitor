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

import asyncio
import argparse
import signal

#=============================================================================
import logging
log = logging.getLogger('performancemonitor-cli')
log.addHandler(logging.StreamHandler())
log.setLevel(logging.INFO)

#=============================================================================
class Program:
	_stop = False
	args = None

	def parse_args(self):
		parser = argparse.ArgumentParser(
			description="Performance monitor client.")
		parser.add_argument('-p', '--port', type=int,
			default=config.get_default_port(),
			help='device')
		parser.add_argument('-l', '--log_level', type=str,
			default='INFO', choices=[ 'DEBUG', 'INFO' ],
			help='log level')
		self.args = parser.parse_args()

		log.setLevel(getattr(logging, self.args.log_level))
		log.debug('Parameters: {}'.format(str(self.args)))

	def main(self):
		self.parse_args()

		for i in ('SIGINT', 'SIGTERM'):
			signal.signal(getattr(signal, i),  lambda signumber, stack, signame=i: self.signal_handler(signame,  signumber, stack) )

		try:
			send_message = lambda message: self.send_message(message)
			asyncio.run(self.client_main('stats'))

		except Exception as e:
			log.error('exception received: {}'.format(str(e)))
			return 1

		return 0

	def stop(self):
		if self._stop: return
		self._stop = True

	def signal_handler(self, signame, signumber, stack):
		log.warning("signal {} received".format(signame))
		self.stop()

	async def client_main(self, message):
		task = asyncio.create_task( self.send_message(message) )
		while not self._stop:
			await asyncio.sleep(0.3)

	async def send_message(self, message):
		reader, writer = await asyncio.open_connection(
			'127.0.0.1', self.args.port )

		while not self._stop:
			log.debug(f'Send: {message!r}')
			writer.write(message.encode())
			await writer.drain()

			data = await reader.read(1024 * 1024)
			log.info(data.decode())

		log.debug(f'Send: close')
		writer.write('stop'.encode())
		await writer.drain()
		log.debug('Close the connection')
		writer.close()
		await writer.wait_closed()

#=============================================================================
if __name__ == '__main__':
	exit( Program().main() )
