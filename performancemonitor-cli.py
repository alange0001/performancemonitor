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

import argparse
import signal
import socket

#=============================================================================
import logging
log = logging.getLogger('performancemonitor-cli')
log.addHandler(logging.StreamHandler())
log.setLevel(logging.INFO)

#=============================================================================
class Program:
	_stop = False
	_sock = None
	args = None

	def parse_args(self):
		parser = argparse.ArgumentParser(
			description="Performance monitor client.")
		parser.add_argument('-p', '--port', type=int,
			default=config.get_default_port(),
			help='device')
		parser.add_argument('-l', '--log_level', type=str,
			default='INFO', choices=[ 'debug', 'DEBUG', 'info', 'INFO' ],
			help='log level')
		self.args = parser.parse_args()

		log.setLevel(getattr(logging, self.args.log_level.upper()))
		log.debug('Parameters: {}'.format(str(self.args)))

	def main(self):
		self.parse_args()

		for i in ('SIGINT', 'SIGTERM'):
			signal.signal(getattr(signal, i),  lambda signumber, stack, signame=i: self.signal_handler(signame,  signumber, stack) )

		try:
			self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			self._sock.connect(('localhost', self.args.port))
			while not self._stop:
				self._sock.sendall(bytes('stats', 'utf-8'))
				response = str(self._sock.recv(1024 * 1024), 'utf-8')
				log.info(response)

		except Exception as e:
			log.error('exception received: {}'.format(str(e)))
			self.stop()
			return 1

		self.stop()
		return 0

	def stop(self):
		if self._stop: return
		self._stop = True
		if self._sock is not None:
			self._sock.close()
			self._sock = None

	def signal_handler(self, signame, signumber, stack):
		log.warning("signal {} received".format(signame))
		self.stop()
		exit(0)

#=============================================================================
if __name__ == '__main__':
	exit( Program().main() )
