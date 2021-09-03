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
import logging

# =============================================================================
version = 1.4
# =============================================================================
log = logging.getLogger('performancemonitor')
log.setLevel(logging.INFO)


# =============================================================================
class ArgsWrapper:  # single global instance "args"
	def get_args(self):
		parser = argparse.ArgumentParser(
			description="Performance monitor server.")
		parser.add_argument('-p', '--port', type=int,
			default=config.get_default_port(),
			help='device')
		parser.add_argument('-i', '--interval', type=int,
			default=config.get_default_interval(),
			help='stats interval')
		parser.add_argument('-d', '--device', type=str,
			default=config.get_default_device(),
			help='disk device name')
		parser.add_argument('-r', '--chroot', type=str,
			default=config.get_default_chroot(),
			help='chroot used to access /proc and /sys')
		parser.add_argument('-s', '--smart', type=str,
			default=config.get_default_smart(), nargs='?',
			help='smartctl data (root required, true|false). Default false.')
		parser.add_argument('--iostat', type=str,
			default=config.get_default_iostat(), nargs='?',
			help='use iostat (true|false). Default: true.')
		parser.add_argument('-o', '--log_handler', type=str,
			default='stderr', choices=[ 'journal', 'stderr' ],
			help='log handler')
		parser.add_argument('-l', '--log_level', type=str,
			default=config.get_default_log_level(), choices=[ 'debug', 'info' ],
			help='log level')
		parser.add_argument('-t', '--test', type=str,
			default='',
			help='test routines')
		args = parser.parse_args()

		args.smart = self._str_as_bool(args.smart, default=True)
		args.iostat = self._str_as_bool(args.iostat, default=True)

		if args.interval < 1:
			raise Exception(f'parameter error: invalid interval: {args.interval}')
		args.device = args.device.split('/')[-1]
		if args.device == '':
			raise Exception(f'parameter error: invalid disk device name: "{args.device}"')

		if args.log_handler == 'journal':
			from systemd.journal import JournalHandler
			log_h = JournalHandler()
			log_h.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
		else:
			log_h = logging.StreamHandler()
			log_h.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
		log.addHandler(log_h)

		log.setLevel(getattr(logging, args.log_level.upper()))
		log.info('Parameters: {}'.format(str(args)))
		return args

	def _str_as_bool(self, value:str, default=False, invalid=False):
		if value is None:
			return default
		ev = value.strip().lower()
		if ev == '':
			return default
		if ev in ['1', 't', 'true', 'y', 'yes']:
			return True
		if ev in ['0', 'f', 'false', 'n', 'no']:
			return False
		return invalid

	def __getattr__(self, name):
		global args
		args = self.get_args()
		return getattr(args, name)


args = ArgsWrapper()


# =============================================================================
class Program: # single instance
	_stop = False

	def main(self):
		ret = 0
		try:
			log.info(f"performancemonitor version {version}")
			for i in ('SIGINT', 'SIGTERM'):
				signal.signal(getattr(signal, i),
				              lambda signumber, stack, signame=i: self.signal_handler(signame, signumber, stack))

			get_sys_info()

			Stats.check_paths()

			IoStat().start()
			time.sleep(1)

			CmdServer.start(self)

			while not self._stop:
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
			IoStat.stop()

	def signal_handler(self, signame, signumber, stack):
		log.warning(f"signal {signame} received")
		self.stop()

	def client_handler(self, handlerObj):  # called by CmdServer.ThreadedTCPRequestHandler
		cur_thread = threading.current_thread()
		log.info(f"{cur_thread.name}: Client handler initiated")
		try:
			old_stats, new_stats = None, Stats()

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
					old_stats, new_stats = new_stats, Stats()
					write_message = bytes(str(StatReport(new_stats, old_stats)), 'utf-8')

					log.debug("Sending stats...")
					handlerObj.request.sendall(write_message)

				elif message == 'reset':
					old_stats, new_stats = new_stats, Stats()
					log.info("Reset stats...")

				elif message == 'stop' or message == 'close' or message == '':
					log.info(f"{cur_thread.name}: command {message} received")
					handlerObj.request.sendall(bytes('OK, stopping', 'utf-8'))
					break

				else:
					ret_msg = f"invalid command: {message}"
					log.error(ret_msg)
					handlerObj.request.sendall(bytes(ret_msg, 'utf-8'))

		except Exception as e:
			if log.level == logging.DEBUG:
				exc_type, exc_value, exc_traceback = sys.exc_info()
				log.debug(f'{cur_thread.name}: Exception:\n' +
				          ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback)))
			log.error(f"{cur_thread.name}: Exception received from client_handler: {str(e)}")

		log.info(f"{cur_thread.name}: Connection closed")


# =============================================================================
class CmdServer:  # single instance
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
			self._program.client_handler(self)

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


# =============================================================================
class StatReport:
	_delta_t = None
	_data = None

	def __init__(self, st_new, st_old):
		self._data = collections.OrderedDict()

		log.debug(f'StatReport')
		if st_new.raw_data['time'] == st_old.raw_data['time']:
			raise Exception('old and new stats are the same')

		self._delta_t = (st_new.raw_data['time'] - st_old.raw_data['time']).total_seconds()

		self._data['time_system']   = st_new.raw_data['time_system']
		self._data['time_system_s'] = st_new.raw_data['time_system_s']
		self._data['time_delta']    = self._delta_t
		log.debug(f'StatReport _delta_t = {self._delta_t}')

		self._data['perfmon_version'] = version
		self._data['system_info'] = get_sys_info()

		self._data['arg_device'] = args.device

		self._data['cpu'] = collections.OrderedDict()
		for k in ['cores', 'threads', 'count']:
			self._data['cpu'][k]   = st_new.raw_data['cpu'][k]
		for k in ['times_total', 'times']:
			self._data['cpu'][k] = to_dict(st_new.raw_data['cpu'][k])

		self._data['cpu']['percent_total'] = self._get_percent(
				st_new.raw_data['cpu']['times_total']._asdict(),
				st_old.raw_data['cpu']['times_total']._asdict())

		self._data['cpu']['percent'] = []
		for i in range(0, len(st_new.raw_data['cpu']['times'])):
			self._data['cpu']['percent'].append( self._get_percent(
					st_new.raw_data['cpu']['times'][i]._asdict(),
					st_old.raw_data['cpu']['times'][i]._asdict()) )

		self._data['cpu']['idle_time_names'] = ['idle', 'iowait', 'steal']

		self._data['disk'] = collections.OrderedDict()
		iostat = IoStat.get_stats(max(1, int(round(self._delta_t))))
		if iostat is not None:
			self._data['disk']['iostat'] = iostat

		if st_new.raw_data['disk'].get('diskstats') is not None and st_old.raw_data['disk'].get('diskstats') is not None:
			rep = collections.OrderedDict()
			self._data['disk']['diskstats'] = rep
			diskstats_new = st_new.raw_data['disk']['diskstats']
			diskstats_old = st_old.raw_data['disk']['diskstats']
			sector_size = st_new.raw_data['disk']['sector_size']

			for kr, kd in [
						   ('r/s', 'read_count'), ('w/s', 'write_count'),
						   ('r_sectors/s', 'read_sectors'), ('w_sectors/s', 'write_sectors')]:
				rep[kr] = (diskstats_new[kd] - diskstats_old[kd]) / self._delta_t

			rep['rkB/s'] = ((diskstats_new['read_sectors'] - diskstats_old['read_sectors']) * sector_size) / (self._delta_t * 1024)
			rep['wkB/s'] = ((diskstats_new['write_sectors'] - diskstats_old['write_sectors']) * sector_size) / (self._delta_t * 1024)

			for k in ['read_time_ms', 'write_time_ms', 'io_time_ms', 'io_time_weighted_ms']:
				rep[k] = diskstats_new[k] - diskstats_old[k]
			rep['cur_ios'] = diskstats_new['cur_ios']

		if st_new.raw_data['disk'].get('scheduler') is not None:
			self._data['disk']['scheduler'] = st_new.raw_data['disk']['scheduler']

		if st_new.raw_data.get('fs') is not None:
			self._data['fs'] = st_new.raw_data['fs']

		self._data['containers'] = collections.OrderedDict()
		containers = st_new.raw_data['containers']
		for c_name, c_data in containers.items():
			report_data = { 'name': c_name,
					   'id':   c_data['ID'], }
			self._data['containers'][c_name] = report_data

			old_c_data = get_recursive(st_old.raw_data, 'containers', c_name)
			if old_c_data is not None:
				for diff_name in ('blkio.service_bytes', 'blkio.serviced'):
					if c_data.get(diff_name) is None or old_c_data.get(diff_name) is None:
						continue
					rep_diff_name = f'{diff_name}/s'
					report_data[rep_diff_name] = {}
					report_data[diff_name] = c_data[diff_name]
					for k, v in c_data[diff_name].items():
						if old_c_data[diff_name].get(k) is None:
							log.warning(f'container {c_name} has no old data [{diff_name}][{k}]')
							continue
						report_data[rep_diff_name][k] = (float(c_data[diff_name][k]) - float(old_c_data[diff_name][k])) / self._delta_t
					# log.debug(f'StatReport container {c_name}, {diff_name}    : {c_data[diff_name]}')
					# log.debug(f'StatReport container {c_name}, {diff_name} old: {old_c_data[diff_name]}')
					# log.debug(f'StatReport container {c_name}, {diff_name}/s  : {report_data[rep_diff_name]}')

		if args.smart:
			diff_keys = ['units_read', 'units_written', 'read_commands', 'write_commands']
			smart_data = collections.OrderedDict()
			self._data['smart'] = smart_data
			for k, v in st_new.raw_data['smart'].items():
				if k in diff_keys:
					old_v = get_recursive(st_old.raw_data, 'smart', k)
					if old_v is not None:
						smart_data[f'{k}/s'] = (float(v) - float(old_v)) / self._delta_t
				else:
					smart_data[k] = v
			if args.log_level == 'debug':
				log.debug(f'StatReport smart : {self._data["smart"]}')

		if args.log_level == 'debug' and args.iostat:
			try:
				log.debug(f'StatReport iostat   : r/s={self._data["disk"]["iostat"]["r/s"]}, w/s={self._data["disk"]["iostat"]["w/s"]}')
				log.debug(f'StatReport diskstats: r/s={self._data["disk"]["diskstats"]["r/s"]}, w/s={self._data["disk"]["diskstats"]["w/s"]}')
				log.debug(f'StatReport iostat   : read KB/s={self._data["disk"]["iostat"]["rkB/s"]}, write KB/s={self._data["disk"]["iostat"]["wkB/s"]}')
				log.debug(f'StatReport diskstats: read KB/s={self._data["disk"]["diskstats"]["rkB/s"]}, write KB/s={self._data["disk"]["diskstats"]["wkB/s"]}')
			except Exception as e:
				exc_type, exc_value, exc_traceback = sys.exc_info()
				sys.stderr.write('report debug exception:\n' + ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback)) + '\n')

	def _get_percent(self, aux_new, aux_old):
		diffs = collections.OrderedDict()
		ret = collections.OrderedDict()
		for k, v in aux_new.items():
			diffs[k] = v - aux_old[k]
		aux_sum = sum(diffs.values())
		for k, v in diffs.items():
			ret[k] = v/aux_sum  * 100.
		return ret

	def __str__(self):
		return 'STATS: {}'.format(json.dumps(self._data))


# =============================================================================
class Stats:
	raw_data = None

	@classmethod
	def check_paths(cls):
		for check_path in [
			f'{args.chroot}/sys/fs/cgroup/blkio',
			f'{args.chroot}/sys/block/{args.device}/queue',
			f'{args.chroot}/proc/diskstats',
		]:
			try:
				os.stat(check_path)
			except Exception as e:
				log.error(f'Failed to access path "{check_path}". Possible incomplete stats.')

	def __init__(self):
		self.raw_data = collections.OrderedDict()
		self._get_time()
		self._get_cpu()
		self._get_disk()
		self._get_fs()
		self._get_containers()
		self._get_smart()

	def _get_time(self):
		t = datetime.datetime.now()
		self.raw_data['time'] = t
		self.raw_data['time_system'] = t.strftime('%Y-%m-%d %H:%M:%S')
		self.raw_data['time_system_s'] = int(t.strftime('%s'))

	def _get_cpu(self):
		self.raw_data['cpu'] = collections.OrderedDict()

		self.raw_data['cpu']['cores']   = psutil.cpu_count(logical=False)
		self.raw_data['cpu']['threads'] = psutil.cpu_count(logical=True)
		self.raw_data['cpu']['count']   = self.raw_data['cpu']['threads']

		self.raw_data['cpu']['times_total'] = psutil.cpu_times()
		self.raw_data['cpu']['times']       = psutil.cpu_times(percpu=True)

	def _get_disk(self):
		self.raw_data['disk'] = collections.OrderedDict()

		exitcode, output = subprocess.getstatusoutput(f"cat {args.chroot}/sys/block/{args.device}/queue/hw_sector_size")
		if exitcode != 0:
			raise Exception(f'failed to get the sector size of device {args.device}')
		self.raw_data['disk']['sector_size'] = int(output)

		exitcode, output = subprocess.getstatusoutput(f"grep '{args.device} ' {args.chroot}/proc/diskstats")
		if exitcode != 0:
			raise Exception(f'could not get diskstats from device {args.device}')
		values = re.findall(r'(\w+)', output)
		if len(values) < 18:
			raise Exception(f'diskstats from device {args.device} are incomplete')
		c, d = 3, collections.OrderedDict()
		for n in ['read_count',  'read_merges',  'read_sectors',  'read_time_ms',
		          'write_count', 'write_merges', 'write_sectors', 'write_time_ms',
		          'cur_ios', 'io_time_ms', 'io_time_weighted_ms']:
			if len(values) <= c: break
			d[n] = int(values[c])
			c += 1
		self.raw_data['disk']['diskstats'] = d

		with open(f'{args.chroot}/sys/block/{args.device}/queue/scheduler', 'r') as schedfile:
			s = re.findall(r'\[([^\]]+)\]', schedfile.readlines()[0])
			if len(s) > 0:
				self.raw_data['disk']['scheduler'] = s[0]

	def _get_fs(self):
		dev_re = f'(/dev/){{0,1}}{args.device}'
		self.raw_data['fs'] = collections.OrderedDict()
		aux = psutil.disk_partitions()
		for m in aux:
			if len(re.findall(dev_re, m.device)) > 0:
				self.raw_data['fs'][m.mountpoint] = collections.OrderedDict()
				for k, v in to_dict(m).items():
					if k != 'mountpoint':
						self.raw_data['fs'][m.mountpoint][k] = v

				# total = 461708984320, used = 242486398976, free = 217889386496, percent = 52.7
				for k, v in to_dict(psutil.disk_usage(m.mountpoint)).items():
					self.raw_data['fs'][m.mountpoint][k] = v

	def _get_containers(self):
		containers = Containers().raw_data()
		self.raw_data['containers'] = containers

	def _get_smart(self):
		def get_val(_data: dict, _key: str, _output: str, _pattern: str, _type=str, _remove_separator: bool = False) -> None:
			aux = re.findall(_pattern, _output)
			# log.debug(f'_get_smart(): v = {v}')
			if len(aux) > 0:
				if _remove_separator:
					_data[_key] = _type(aux[0].replace('.', '').replace(',', ''))
				else:
					_data[_key] = _type(aux[0])

		if args.smart:
			cmd = f'smartctl -a "/dev/{args.device}"'
			data = collections.OrderedDict()
			self.raw_data['smart'] = data

			exitcode, output = subprocess.getstatusoutput(cmd)
			if exitcode != 0:
				raise Exception(f'smartctl returned error {exitcode}')

			get_val(data, 'model',            output, r'Model Number: +(.+)',                    str)
			get_val(data, 'serial',           output, r'Serial Number: +(.+)',                   str)
			get_val(data, 'firmware',         output, r'Firmware Version: +(.+)',                str)
			get_val(data, 'lifetime_percent', output, r'Percentage Used: +([0-9]+)',             int)
			get_val(data, 'capacity',         output, r'Namespace 1 Size/Capacity: +([0-9.,]+)', int, True)
			get_val(data, 'utilization',      output, r'Namespace 1 Utilization: +([0-9.,]+)',   int, True)
			get_val(data, 'temperature',      output, r'Temperature: +([0-9]+)',                 int)
			get_val(data, 'units_read',       output, r'Data Units Read: +([0-9.,]+)',           int, True)
			get_val(data, 'units_written',    output, r'Data Units Written: +([0-9.,]+)',        int, True)
			get_val(data, 'read_commands',    output, r'Host Read Commands: +([0-9.,]+)',        int, True)
			get_val(data, 'write_commands',   output, r'Host Write Commands: +([0-9.,]+)',       int, True)

	# log.debug(f'_get_smart(): data = {data}')

	def __str__(self):
		return 'STATS: {}'.format(json.dumps(self.raw_data))


# =============================================================================
class IoStat (threading.Thread):  # single instance
	_self_ = None
	_device = None
	_interval = None
	_started_ = False
	_stop_ = False
	_exception = None
	_proc = None
	_stats_lock = None
	_stats = None

	def __init__(self):
		if self.__class__._self_ is not None:
			raise Exception('class IoStat initiated twice')
		self.__class__._self_ = self

		threading.Thread.__init__(self)
		self.name = 'iostat'
		self._device = args.device
		self._interval = 1
		self._stats = []
		self._stats_lock = threading.Lock()

	def run(self):
		if not args.iostat:
			return
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
						with self._stats_lock:
							while len(self._stats) > 60:
								del self._stats[0]
							self._stats.append(j)
						# log.debug(f'iostat: rkB/s={j["rkB/s"]}, wkB/s={j["wkB/s"]}')
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
	def get_stats(cls, interval: int):
		if not args.iostat:
			return None
		if cls._self_ is None:
			log.warning('IOstat not initiated')
			return None

		self = cls._self_
		if not self._stop_:
			self.check()

		ret = collections.OrderedDict()
		with self._stats_lock:
			aux_interval = min(interval, len(self._stats))
			if aux_interval < interval:
				log.warning(f'requested iostat interval ({interval}) is greater than available ({aux_interval})')
			if aux_interval == 0:
				return None
			# log.debug(f'IoStats.get_stats: aux_interval = {aux_interval}')
			ret['disk_device'] = self._stats[-1]['disk_device']
			for k in self._stats[-1].keys():
				if k == 'disk_device': continue
				aux_sum = sum([self._stats[i][k] for i in range(-1, -1*aux_interval -1, -1)])
				# log.debug(f'IoStats.get_stats: k = {k}, aux_sum = {aux_sum}')
				ret[k] = aux_sum / float(aux_interval)
		return ret

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


# =============================================================================
class Partitions:
	data = None
	data_major_minor = None

	def __init__(self):
		if self.__class__.data is None:
			log.debug('Partitions: load partitions')
			self.__class__.data = {}
			self.__class__.data_major_minor = {}
			with open(f'{args.chroot}/proc/partitions', 'r') as fd:
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


# =============================================================================
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
		# log.debug(f'major_minor = {major_minor}')

		self._container_names = {}
		self._container_ids   = {}
		for l in output.splitlines():
			j = json.loads(l)
			id = j['ID']
			self._container_names[j['Names']] = j
			self._container_ids[id] = j

			try:
				cgroup_path = self._get_cgroup_blkio_path(id)

				for k, fname in [('blkio.service_bytes', 'blkio.throttle.io_service_bytes'),
								 ('blkio.serviced', 'blkio.throttle.io_serviced')]:
					fpath = os.path.join(cgroup_path, fname)
					try:
						if os.path.isfile(fpath):
							j[k] = self._get_blkio(major_minor, fpath)
							# log.debug(f'Container {j.get("Names")} [{k}]: {j[k]}')
						else:
							log.error(f'Container {j.get("Names")}: file {fpath} does not exist')

					except Exception as e:
						log.error(f'Container {j.get("Names")} exception: {str(e)}')

			except Exception as e:
				log.error(f'Container {j.get("Names")} exception: {str(e)}')

	def _get_cgroup_blkio_path(self, container_id):
		base_directory = f'{args.chroot}/sys/fs/cgroup/blkio/docker'
		for i in os.listdir(base_directory):
			aux = os.path.join(base_directory, i)
			if i.find(container_id) == 0 and os.path.isdir(aux):
				return aux
		raise Exception(f'directory "{base_directory}/{container_id}*" not found')

	def _get_blkio(self, major_minor, filename):
		ret = {}
		with open(filename, 'rt') as f:
			for l in f:
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


# =============================================================================
class Test:
	def __init__(self, name):
		f = getattr(self, name)
		if f is None:
			raise Exception(f'test named "{name}" does not exist')
		f()

	def args(self):
		log.info(f'{self.__class__.__name__}.args()')
		log.info(args)

	def stats(self):
		log.info(f'{self.__class__.__name__}.stats()')
		IoStat().start()

		old_stats, new_stats = None, Stats()
		while True:
			time.sleep(args.interval)
			old_stats, new_stats = new_stats, Stats()
			log.info(StatReport(new_stats, old_stats))

	def iostat(self):
		log.info(f'{self.__class__.__name__}.iostat()')
		IoStat().start()
		while True:
			time.sleep(args.interval)
			log.info(IoStat.get_stats(args.interval))

	def partitions(self):
		log.info(f'{self.__class__.__name__}.partitions()')
		p = Partitions()
		log.info(p.data)

	def containers(self):
		log.info(f'{self.__class__.__name__}.containers()')
		d = Containers()
		log.info(d.names())
		log.info(d.ids())
		log.info(d._container_names)

	def sys_info(self):
		log.info(f'{self.__class__.__name__}.containers()')
		print(get_sys_info())


# =============================================================================
def get_recursive(value, *attributes):
	cur_v = value
	for i in attributes:
		try:
			cur_v = cur_v[i]
		except:
			return None
	return cur_v


# =============================================================================
def to_dict(data):
	base_types = (str, int, float, complex, bool)
	if isinstance(data, base_types):
		return data

	if isinstance(data, list):
		ret = []
		for v in data:
			ret.append(to_dict(v))
		return ret

	if isinstance(data, dict):
		ret = collections.OrderedDict()
		for k, v in data.items():
			ret[k] = to_dict(v)
		return ret

	d = dir(data)
	if '_asdict' in d:
		return data._asdict()

	ret = collections.OrderedDict()
	for key in d:
		value = getattr(data, key)
		if key[0] != '_' and isinstance (value, base_types):
			ret[key] = value
	return ret


# =============================================================================
_sys_info = None
def get_sys_info() -> dict:
	global _sys_info
	if _sys_info is None:
		ret = collections.OrderedDict()
		for k in ['all', 'kernel-name', 'nodename', 'kernel-release', 'kernel-version', 'machine']:
			st, out = subprocess.getstatusoutput(f'uname --{k}')
			if st != 0:
				raise Exception('failed to read the system information (uname -a)')
			ret[k] = out
		_sys_info = ret

	return _sys_info


# =============================================================================
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
			sys.stderr.write('main exception:\n' +
			                 ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback)) + '\n')
		else:
			sys.stderr.write(str(e) + '\n')
		exit(1)

	exit(r)
