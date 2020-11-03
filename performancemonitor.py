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
import sys
import os
import subprocess
import shlex
import datetime
import collections
import json
import re
import traceback

#=============================================================================
import logging
from systemd.journal import JournalHandler
log = logging.getLogger('performancemonitor')
log.setLevel(logging.INFO)

#=============================================================================
class ArgsWrapper: # single global instance "args"
	def get_args(self):
		parser = argparse.ArgumentParser(
			description="Performance monitor server.")
		parser.add_argument('-p', '--port', type=int,
			default=config.get_default_port(),
			help='device')
		parser.add_argument('-i', '--interval', type=int,
			default=5,
			help='stats interval')
		parser.add_argument('-d', '--device', type=str,
			default='sda',
			help='disk device name')
		parser.add_argument('-o', '--log_handler', type=str,
			default='stderr', choices=[ 'journal', 'stderr' ],
			help='log handler')
		parser.add_argument('-l', '--log_level', type=str,
			default='INFO', choices=[ 'debug', 'DEBUG', 'info', 'INFO' ],
			help='log level')
		parser.add_argument('-t', '--test', type=str,
			default='',
			help='test routines')
		args = parser.parse_args()

		if args.interval < 1:
			raise Exception(f'parameter error: invalid interval: {args.interval}')
		args.device = args.device.split('/')[-1]
		if args.device == '':
			raise Exception(f'parameter error: invalid disk device name: "{args.device}"')

		if args.log_handler == 'journal':
			log_h = JournalHandler()
			log_h.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
		else:
			log_h = logging.StreamHandler()
			log_h.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
		log.addHandler(log_h)

		log.setLevel(getattr(logging, args.log_level.upper()))
		log.info('Parameters: {}'.format(str(args)))
		return args

	def __getattr__(self, name):
		global args
		args = self.get_args()
		return getattr(args, name)

args = ArgsWrapper()

#=============================================================================
class Program: # single instance
	_stop = False
	_st_list = None
	_st_list_lock = None

	def main(self):
		ret = 0
		try:
			log.info("Program initiated")
			for i in ('SIGINT', 'SIGTERM'):
				signal.signal(getattr(signal, i),  lambda signumber, stack, signame=i: self.signalHandler(signame,  signumber, stack) )

			self._st_list = []
			self._st_list_lock = threading.Lock()

			iostat.getStats()
			self.collectStats()
			time.sleep(1)

			CmdServer.start(self)

			while not self._stop:
				self.collectStats()
				time.sleep(1)

		except Exception as e:
			if log.level == logging.DEBUG:
				exc_type, exc_value, exc_traceback = sys.exc_info()
				log.critical('exception received:\n' + ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback)))
			else:
				log.critical("exception received: {}".format(str(e)))
			ret = 1

		self.stop()
		return ret

	def stop(self):
		if not self._stop:
			self._stop = True
			CmdServer.stop()
			iostat.stop()

	def signalHandler(self, signame, signumber, stack):
		log.warning("signal {} received".format(signame))
		self.stop()

	def collectStats(self):
		with self._st_list_lock:
			st = Stats()
			sl = self._st_list[:]
			sl.append(st)
			if len(sl) > args.interval:
				del sl[0]
			self._st_list = sl

	def currentStats(self):
		return self._st_list

	def resetStats(self):
		with self._st_list_lock:
			self._st_list = []

	def clientHandler(self, handlerObj): # called by CmdServer.ThreadedTCPRequestHandler
		cur_thread = threading.current_thread()
		log.info(f"{cur_thread.name}: Client handler initiated")
		try:
			while True:
				message = str(handlerObj.request.recv(1024), 'utf-8').strip()
				log.debug(f"{cur_thread.name}: message \"{message}\" received")

				if self._stop:
					break

				if message == 'alive':
					write_message = bytes('yes', 'utf-8')
					log.debug("Sending yes...")
					handlerObj.request.sendall(write_message)

				elif message == 'stats':
					st_list = self.currentStats()
					write_message = bytes(str(StatReport(st_list)), 'utf-8')

					log.debug("Sending stats...")
					handlerObj.request.sendall(write_message)

				elif message == 'reset':
					self.resetStats()
					log.info("Reset stats...")

				elif message == 'stop' or message == 'close' or message == '':
					log.info(f"{cur_thread.name}: command {message} received")
					handlerObj.request.sendall(bytes('OK, stopping', 'utf-8'))
					break

				else:
					raise Exception(f"invalid command: {message}")

		except Exception as e:
			log.error(f"{cur_thread.name}: Exception received from clientHandler: {str(e)}")

		log.info(f"{cur_thread.name}: Connection closed")

#=============================================================================
class CmdServer: # single instance
	_self_         = None
	_stop          = False
	_server        = None
	_server_thread = None

	# https://docs.python.org/3/library/socketserver.html
	class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
		pass
	class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):
		_program = None
		def handle(self):
			self._program.clientHandler(self)

	def __init__(self, program):
		self.ThreadedTCPRequestHandler._program = program

		host_port = ('localhost', args.port)
		self._server = self.ThreadedTCPServer(host_port, self.ThreadedTCPRequestHandler)
		self._server_thread = threading.Thread(target=self._server.serve_forever)
		self._server_thread.daemon = True
		log.info(f'Starting {self.__class__.__name__} {host_port}')
		self._server_thread.start()
		log.debug(f'{self.__class__.__name__} started')

	@classmethod
	def start(cls, program):
		cls._self_ = CmdServer(program)

	@classmethod
	def stop(cls):
		self = cls._self_
		if self is not None and not self._stop:
			cls._self_ = None
			self._stop = True
			log.info(f'Stopping {self.__class__.__name__}')
			self._server.shutdown()

#=============================================================================
class StatReport:
	_delta_t = None
	_data = None

	def __init__(self, st_list):
		self._data = collections.OrderedDict()
		if st_list is not None and len(st_list) > 0:
			st_new, st_old = st_list[-1], st_list[0]

			self._delta_t = (st_new._raw_data['time'] - st_old._raw_data['time']).total_seconds()

			self._data['time_system']   = st_new._raw_data['time_system']
			self._data['time_system_s'] = st_new._raw_data['time_system_s']
			self._data['time_delta']    = self._delta_t

			self._data['arg_device'] = args.device

			self._data['cpu'] = collections.OrderedDict()
			for k in ['cores', 'threads', 'count']:
				self._data['cpu'][k]   = st_new._raw_data['cpu'][k]
			for k in ['times_total', 'times']:
				self._data['cpu'][k] = self._toDict(st_new._raw_data['cpu'][k])

			if len(st_list) > 1:
				self._data['cpu']['percent_total'] = self._getPercent(
						st_new._raw_data['cpu']['times_total']._asdict(),
						st_old._raw_data['cpu']['times_total']._asdict())

				self._data['cpu']['percent'] = []
				for i in range(0, len(st_new._raw_data['cpu']['times'])):
					self._data['cpu']['times'].append( self._getPercent(
							st_new._raw_data['cpu']['times'][i]._asdict(),
							st_old._raw_data['cpu']['times'][i]._asdict()) )

			self._data['cpu']['idle_time_names'] = [ 'idle', 'iowait', 'steal' ]

			self._data['disk'] = collections.OrderedDict()
			iostat = st_new._raw_data['disk'].get('iostat')
			if iostat is not None:
				self._data['disk']['iostat'] = collections.OrderedDict()
				for k in iostat.keys():
					if k == 'disk_device': continue
					count, sum_k = 0, 0
					for st in st_list:
						if st._raw_data['disk'].get('iostat') is not None:
							count += 1
							sum_k = st._raw_data['disk']['iostat'][k]
					#log.debug(f'iostat key={k}, sum={sum_k}, count={count}')
					self._data['disk']['iostat'][k] = sum_k/count if count > 0 else 0

			self._data['containers'] = collections.OrderedDict()
			containers = st_new._raw_data['containers']
			for name, data in containers.items():
				i_data = { 'name': name,
				           'id':   data['ID'], }
				self._data['containers'][name] = i_data

				if  st_old._raw_data is not None \
				and st_old._raw_data.get('containers') is not None \
				and st_old._raw_data['containers'].get(name) is not None:
					old_data = st_old._raw_data['containers'][name]

					for diff_name in ('blkio.service_bytes', 'blkio.serviced'):
						if data.get(diff_name) is None or old_data.get(diff_name) is None:
							continue
						i_data[f'{diff_name}/s'] = {}
						for k, v in data[diff_name].items():
							i_data[f'{diff_name}/s'][k] = (float(data[diff_name][k]) - float(old_data[diff_name][k])) / self._delta_t
						log.debug(f'container {name}, {diff_name}    : {data[diff_name]}')
						log.debug(f'container {name}, {diff_name} old: {old_data[diff_name]}')
						log.debug(f'container {name}, {diff_name}/s: {i_data[f"{diff_name}/s"]}')

	def _getPercent(self, aux_new, aux_old):
		diffs = collections.OrderedDict()
		ret = collections.OrderedDict()
		for k, v in aux_new.items():
			diffs[k] = v - aux_old[k]
		aux_sum = sum(diffs.values())
		for k, v in diffs.items():
			ret[k] = v/aux_sum
		return ret

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

	def __str__(self):
		return 'STATS: {}'.format(json.dumps(self._data))

#=============================================================================
class Stats:
	_raw_data = None

	def __init__(self):
		self._raw_data = collections.OrderedDict()

		self.getTime()
		self.getCPU()
		self.getDisk()
		#self.getFS()
		self.getContainers()

	def getTime(self):
		t = datetime.datetime.now()
		self._raw_data['time'] = t
		self._raw_data['time_system'] = t.strftime('%Y-%m-%d %H:%M:%S')
		self._raw_data['time_system_s'] = int(t.strftime('%s'))

	def getCPU(self):
		self._raw_data['cpu'] = collections.OrderedDict()

		self._raw_data['cpu']['cores']   = psutil.cpu_count(logical=False)
		self._raw_data['cpu']['threads'] = psutil.cpu_count(logical=True)
		self._raw_data['cpu']['count']   = self._raw_data['cpu']['threads']

		self._raw_data['cpu']['times_total'] = psutil.cpu_times()
		self._raw_data['cpu']['times']       = psutil.cpu_times(percpu=True)

	def getDisk(self):
		self._raw_data['disk'] = collections.OrderedDict()
		counters = psutil.disk_io_counters(perdisk=True)
		if counters.get(args.device) is None:
			raise Exception(f'could not get stats from device {args.device}')
		self._raw_data['disk']['counters'] = counters[args.device]

		st_io = iostat.getStats()
		if st_io is not None:
			self._raw_data['disk']['iostat'] = st_io
		else:
			log.warning('iostat has no data')

	def getFS(self):
		dev_re = f'(/dev/){{0,1}}{self._disk_device}[0-9]+'
		self._data['fs'] = collections.OrderedDict()
		self._data['fs']['mount'] = []
		aux = self._toDict(psutil.disk_partitions())
		for m in aux:
			if len( re.findall(dev_re, m['device']) ) > 0:
				self._data['fs']['mount'].append(m)

		self._data['fs']['statvfs'] = collections.OrderedDict()
		dev_paths = set([x['device'] for x in self._data['fs']['mount']])
		for d in dev_paths:
			if len( re.findall(dev_re, d) ) > 0:
				for m in self._data['fs']['mount']:
					if m['device'] == d:
						self._data['fs']['statvfs'][d] = self._toDict(os.statvfs(m['mountpoint']))
						break

	def getContainers(self):
		containers = Containers().raw_data()
		self._raw_data['containers'] = containers

	def __str__(self):
		return 'STATS: {}'.format(json.dumps(self._data))

#=============================================================================
class iostat (threading.Thread): # single instance
	_self_ = None
	_device = None
	_interval = None
	_started_ = False
	_stop_ = False
	_exception = None
	_proc = None
	_stats = None
	def __init__(self):
		threading.Thread.__init__(self)
		self.name = 'iostat'
		self._device = args.device
		self._interval = 1

	def run(self):
		log.info('Starting subprocess iostat...')
		cmd = shlex.split(f'iostat -xky -o JSON {self._interval} {self._device}')
		try:
			with subprocess.Popen(
				cmd, stdout=subprocess.PIPE, shell=False,
				stderr=subprocess.STDOUT) as proc:
				self._proc = proc
				self._started_ = True

				for l in iter(proc.stdout.readline, b''):
					s = l.decode('utf-8')
					r = re.findall(r'(\{"disk_device"[^}]+\})', s)
					if len(r) > 0:
						j = json.loads(r[0])
						self._stats = j
						#log.debug('iostat: ' + str(j))
					if self._stop_: break
		except Exception as e:
			self._exception = e

		self._proc = None

	def check(self):
		if self._exception is not None:
			raise self._exception
		if self._started_ and self._proc is None:
			raise Exception('iostat is not running')

	@classmethod
	def getStats(cls):
		if cls._self_ is None:
			cls._self_ = iostat()
			cls._self_.start()
			return None

		self = cls._self_
		if not self._stop_:
			self.check()
		return self._stats

	@classmethod
	def stop(cls):
		self = cls._self_
		if self is not None:
			cls._self_ = None
			log.info('Stopping IOstat')
			if self._stop_: return
			self._stop_ = True
			if self._proc is not None:
				self._proc.kill()
				self._proc = None

#=============================================================================
class Partitions:
	data = None
	data_major_minor = None
	def __init__(self):
		if self.__class__.data is None:
			log.debug('Partitions: load partitions')
			self.__class__.data = {}
			self.__class__.data_major_minor = {}
			with open('/proc/partitions', 'r') as fd:
				lines = fd.readlines()
				for l in lines:
					r = re.findall(r'\s([0-9]+)\s+([0-9]+)\s+([0-9]+)\s+(.+)', l)
					if len(r) > 0:
						d = { 'name':  r[0][3],
						      'major': r[0][0],
						      'minor': r[0][1],
						      'blocks':r[0][2], }
						self.__class__.data[d['name']] = d
						self.__class__.data_major_minor[f"{d['major']}:{d['minor']}"] = d

	def __getitem__(self, idx):
		r = self.__class__.data.get(idx)
		if r is not None: return r
		r = self.__class__.data_major_minor[idx]
		if r is not None: return r
		raise Exception(f'partition "{idx}" not found')

#=============================================================================
class Containers:
	_container_names = None
	_container_ids   = None
	def __init__(self):
		cmd = "docker ps --format '{{json . }}'"
		exitcode, output = subprocess.getstatusoutput(cmd)
		if exitcode != 0:
			raise Exception(f'docker returned error {exitcode}')

		partition = Partitions()[args.device]
		major_minor = f'{partition["major"]}:{partition["minor"]}'
		#log.debug(f'major_minor = {major_minor}')

		self._container_names = {}
		self._container_ids   = {}
		for l in output.splitlines():
			j = json.loads(l)
			id = j['ID']
			self._container_names[j['Names']] = j
			self._container_ids[id] = j

			aux = self.get_blkio(major_minor, f'/sys/fs/cgroup/blkio/docker/{id}*/blkio.throttle.io_service_bytes')
			if aux is not None: j['blkio.service_bytes'] = aux

			aux = self.get_blkio(major_minor, f'/sys/fs/cgroup/blkio/docker/{id}*/blkio.throttle.io_serviced')
			if aux is not None: j['blkio.serviced'] = aux

	def get_blkio(self, major_minor, filename):
		ret = {}
		cmd = f"cat {filename}"
		exitcode, output = subprocess.getstatusoutput(cmd)
		if exitcode != 0:
			log.error(f'get_blkio command "{cmd}" returned error {exitcode}')
			return None
		for l in output.splitlines():
			r = re.findall(f'{major_minor} ([^ ]+) (.*)', l)
			if len(r) > 0:
				ret[r[0][0]] = r[0][1]
		return ret

	def names(self):
		return self._container_names.keys()
	def ids(self):
		return self._container_ids.keys()
	def raw_data(self):
		return self._container_names
	def __getitem__(self, idx):
		r = self._container_names.get(idx)
		if r is not None: return r
		return self._container_ids[idx]

#=============================================================================
class Test:
	def __init__(self, name):
		f = getattr(self, name)
		if f is None:
			raise Exception(f'test named "{name}" does not exist')
		f()

	def args(self):
		log.info(args)

	def stats(self):
		iostat.getStats()

		st_list = []
		st_list.append(Stats())
		for i in range(0,4):
			time.sleep(args.interval)
			st_list.append(Stats())
			log.info(StatReport(st_list))
		iostat.stop()

	def iostat(self):
		for i in range(0,2):
			time.sleep(args.interval)
			log.info(iostat.getStats())
		iostat.stop()

	def partitions(self):
		p = Partitions()
		log.info(p.data)

	def containers(self):
		d = Containers()
		log.info(d.names())
		log.info(d.ids())
		log.info(d._container_names)

#=============================================================================
if __name__ == '__main__':
	r = 0
	try:
		if args.test == '':
			r = Program().main()
		else:
			Test(args.test)

		log.info('exit {}'.format(r))

	except Exception as e:
		if log.level == logging.DEBUG:
			exc_type, exc_value, exc_traceback = sys.exc_info()
			sys.stderr.write('main exception:\n' + ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback)) + '\n')
		else:
			sys.stderr.write(str(e) + '\n')
		exit(1)

	exit(r)
