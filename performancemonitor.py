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
import collections
import psutil
import json
import os

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
	_current_stats = None

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
			default='INFO', choices=[ 'debug', 'DEBUG', 'info', 'INFO' ],
			help='log level')
		self.args = parser.parse_args()

		log.setLevel(getattr(logging, self.args.log_level.upper()))
		log.info('Parameters: {}'.format(str(self.args)))

	def main(self):
		try:
			log.info("Program initiated")
			for i in ('SIGINT', 'SIGTERM'):
				signal.signal(getattr(signal, i),  lambda signumber, stack, signame=i: self.signal_handler(signame,  signumber, stack) )

			self.startThread( CmdServer(self) )

			self.collectStats()
			while not self._stop:
				time.sleep(5)
				self.collectStats()

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

	def signal_handler(self, signame, signumber, stack):
		log.warning("signal {} received".format(signame))
		self.stop()

	def collectStats(self):
		s = Stats(self._current_stats)
		self._current_stats = s

	def currentStats(self):
		s = self._current_stats
		return s

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
		try:
			addr = writer.get_extra_info('peername')

			old_stats = self._program.currentStats()

			while not self._stop_thread:
				data = await reader.read(255)
				message = data.decode()
				log.debug(f"Received {message!r} from {addr!r}")

				if message == 'stats':
					cur_stats = old_stats
					while not self._stop_thread and cur_stats.counter() == old_stats.counter():
						await asyncio.sleep(0.3)
						cur_stats = self._program.currentStats()
					if self._stop_thread:
						break

					write_message = str(cur_stats)
					old_stats = cur_stats
					log.debug(f"Send: {write_message!r}")
					writer.write(write_message.encode())
					await writer.drain()
				elif message == 'stop':
					writer.write('OK, stopping'.encode())
					await writer.drain()
					break
				else:
					writer.write('unknown command'.encode())
					await writer.drain()
					break
		except Exception as e:
			log.error('Exception received in CmdServer.handler: {}'.format(str(e)))

		log.debug("Close the connection")
		writer.close()

#=============================================================================
class Stats:
	_counter = None
	_data = None
	_old_data = None

	def __init__(self, old):
		if old is not None:
			self._counter = old._counter+1
			self._old_data = old._data
		else:
			self._counter = 0

		self._data = collections.OrderedDict()

		self.getCPU()
		self.getDisk()
		self.getFS()

	def getCPU(self):
		#idle_times = [ 'idle', 'iowait', 'steal' ]
		def getPercents(new, old):
			sum_total = sum(new.values()) - sum(old.values())
			ret = collections.OrderedDict()
			for k in new.keys():
				ret[k] = 100. * ((new[k]-old[k])/sum_total)
			return ret

		self._data['cpu'] = collections.OrderedDict()
		self._data['cpu']['cores'] = psutil.cpu_count(logical=False)
		self._data['cpu']['threads'] = psutil.cpu_count(logical=True)
		self._data['cpu']['count'] = self._data['cpu']['threads']
		#TODO register only the difference between current and old data
		self._data['cpu']['times_total'] = self._toDict(psutil.cpu_times())
		self._data['cpu']['times'] = self._toDict(psutil.cpu_times(percpu=True))

		if self._old_data is not None:
			self._data['cpu']['percent_total'] = getPercents(self._data['cpu']['times_total'], self._old_data['cpu']['times_total'])
			self._data['cpu']['percent'] = []
			for i in range(0, len(self._data['cpu']['times'])):
				self._data['cpu']['percent'].append(getPercents(self._data['cpu']['times'][i], self._old_data['cpu']['times'][i]))

	def getDisk(self):
		self._data['disk'] = collections.OrderedDict()
		#TODO register only the difference between current and old data
		self._data['disk']['counters'] = self._toDict(psutil.disk_io_counters(perdisk=True))

	def getFS(self):
		self._data['fs'] = collections.OrderedDict()
		self._data['fs']['mount'] = self._toDict(psutil.disk_partitions())

		self._data['fs']['statvfs'] = collections.OrderedDict()
		dev_paths = set([x['device'] for x in self._data['fs']['mount']])
		for d in dev_paths:
			for m in self._data['fs']['mount']:
				if m['device'] == d:
					self._data['fs']['statvfs'][d] = self._toDict(os.statvfs(m['mountpoint']))
					break

	def _toDict(self, data):
		basetypes = (str, int, float, complex, bool)
		if isinstance(data, basetypes):
			return data

		if isinstance(data, list):
			ret = []
			for v in data:
				ret.append(self._toDict(v))
			return ret

		if isinstance(data, dict):
			ret = collections.OrderedDict()
			for k, v in data.items():
				ret[k] = self._toDict(v)
			return ret

		d = dir(data)
		if '_asdict' in d:
			return data._asdict()

		ret = collections.OrderedDict()
		for key in d:
			value = getattr(data, key)
			if key[0] != '_' and isinstance (value, basetypes):
				ret[key] = value
		return ret

	def counter(self): return self._counter

	def __str__(self):
		return 'STATS: {}'.format(json.dumps(self._data))

#=============================================================================
if __name__ == '__main__':
	r = Program().main()
	log.info('exit {}'.format(r))
	exit(r)
