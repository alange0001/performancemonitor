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
import threading
import socket
import socketserver
import time
import psutil
import os
import datetime
import collections
import json

#=============================================================================
import logging
from systemd.journal import JournalHandler
log = logging.getLogger('performancemonitor')
log.addHandler(JournalHandler())
log.setLevel(logging.INFO)

#=============================================================================
class Program:
	_stop = False
	_cmd_thread = None
	_current_stats = None

	args = None

	def __init__(self):
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
				signal.signal(getattr(signal, i),  lambda signumber, stack, signame=i: self.signalHandler(signame,  signumber, stack) )

			self._cmd_thread = CmdServer(self)

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

	def stop(self):
		if self._stop: return
		self._stop = True
		if self._cmd_thread is not None:
			self._cmd_thread.stop()

	def signalHandler(self, signame, signumber, stack):
		log.warning("signal {} received".format(signame))
		self.stop()

	def collectStats(self):
		s = Stats(self._current_stats)
		self._current_stats = s

	def currentStats(self):
		s = self._current_stats
		return s

	def clientHandler(self, handlerObj): #called by CmdServer.ThreadedTCPRequestHandler
		cur_thread = threading.current_thread()
		log.info(f"{cur_thread.name}: Client handler initiated")
		try:
			old_stats = self.currentStats()
			while True:
				message = str(handlerObj.request.recv(1024), 'utf-8')
				log.debug(f"{cur_thread.name}: message \"{message}\" received")
				if message == 'stats':
					cur_stats = old_stats
					while not self._stop and cur_stats.counter() == old_stats.counter():
						time.sleep(0.3)
						cur_stats = self.currentStats()
					if self._stop:
						break

					write_message = bytes(str(cur_stats), 'utf-8')
					old_stats = cur_stats

					log.debug("Sending stats...")
					handlerObj.request.sendall(write_message)

				elif message == 'stop' or message == 'close':
					log.info(f"{cur_thread.name}: command {message} received")
					handlerObj.request.sendall(bytes('OK, stopping', 'utf-8'))
					break
				else:
					raise Exception(f"invalid command: {message}")
		except Exception as e:
			log.error(f"{cur_thread.name}: Exception received: {str(e)}")

		log.info(f"{cur_thread.name}: Connection closed")

#=============================================================================
class CmdServer:
	_stop          = False
	_server        = None
	_server_thread = None

	def __init__(self, program):
		self.ThreadedTCPRequestHandler._program = program

		host_port = ('localhost', program.args.port)
		self._server = self.ThreadedTCPServer(host_port, self.ThreadedTCPRequestHandler)
		self._server_thread = threading.Thread(target=self._server.serve_forever)
		self._server_thread.daemon = True
		log.info(f'Starting {self.__class__.__name__} {host_port}')
		self._server_thread.start()
		log.debug(f'{self.__class__.__name__} started')

	def stop(self):
		if not self._stop:
			log.info(f'Stopping {self.__class__.__name__}')
			self._stop = True
			self._server.shutdown()

	# https://docs.python.org/3/library/socketserver.html
	class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
		pass
	class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):
		_program = None
		def handle(self):
			self._program.clientHandler(self)

#=============================================================================
class Stats:
	_counter = None
	_raw_data = None
	_data = None
	_old_raw_data = None
	_old_data = None

	def __init__(self, old):
		if old is not None:
			self._counter = old._counter+1
			self._old_raw_data = old._raw_data
			self._old_data = old._data
		else:
			self._counter = 0

		self._raw_data = collections.OrderedDict()
		self._data = collections.OrderedDict()

		self.getTime()
		self.getCPU()
		self.getDisk()
		self.getFS()

	def getTime(self):
		self._raw_data['time'] = datetime.datetime.now()
		self._data['system_time'] = self._raw_data['time'].strftime('%Y-%m-%d %H:%M:%S')
		self._data['system_time_s'] = int(self._raw_data['time'].strftime('%s'))

	def getCPU(self):
		#idle_times = [ 'idle', 'iowait', 'steal' ]
		def getPercents(new):
			sum_total = sum(new.values())
			ret = collections.OrderedDict()
			for k in new.keys():
				ret[k] = 100. * (new[k]/sum_total)
			return ret

		self._data['cpu'] = collections.OrderedDict()
		self._raw_data['cpu'] = collections.OrderedDict()

		self._data['cpu']['cores'] = psutil.cpu_count(logical=False)
		self._data['cpu']['threads'] = psutil.cpu_count(logical=True)
		self._data['cpu']['count'] = self._data['cpu']['threads']

		self._raw_data['cpu']['times_total'] = self._toDict(psutil.cpu_times())
		self._raw_data['cpu']['times'] = self._toDict(psutil.cpu_times(percpu=True))

		if self._old_data is not None:
			self._data['cpu']['times_total'] = self._getDiff(self._raw_data['cpu']['times_total'], self._old_raw_data['cpu']['times_total'])
			self._data['cpu']['times'] = []
			for i in range(0, len(self._raw_data['cpu']['times'])):
				self._data['cpu']['times'].append(self._getDiff(self._raw_data['cpu']['times'][i], self._old_raw_data['cpu']['times'][i]))

			self._data['cpu']['percent_total'] = getPercents(self._data['cpu']['times_total'])
			self._data['cpu']['percent'] = []
			for i in range(0, len(self._data['cpu']['times'])):
				self._data['cpu']['percent'].append(getPercents(self._data['cpu']['times'][i]))

	def getDisk(self):
		self._raw_data['disk'] = collections.OrderedDict()
		self._data['disk'] = collections.OrderedDict()
		self._raw_data['disk']['counters'] = self._toDict(psutil.disk_io_counters(perdisk=True))
		if self._old_data is not None:
			self._data['disk']['counters'] = collections.OrderedDict()
			for k, v in self._raw_data['disk']['counters'].items():
				oldv = self._raw_data['disk']['counters'].get(k)
				if oldv is not None:
					self._data['disk']['counters'][k] = self._getDiff(v, oldv)

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

	def _getDiff(self, new, old):
		ret = collections.OrderedDict()
		for k, v in new.items():
			ret[k] = v - old[k]
		return ret

	def counter(self): return self._counter

	def __str__(self):
		return 'STATS: {}'.format(json.dumps(self._data))

#=============================================================================
if __name__ == '__main__':
	r = Program().main()
	log.info('exit {}'.format(r))
	exit(r)
