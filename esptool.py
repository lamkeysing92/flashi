#!/usr/bin/env python3
#
# ESP8266 ROM Bootloader Utility
# https://github.com/themadinventor/esptool
#
# Copyright (C) 2014 Fredrik Ahlberg
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful, but WITHOUT 
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51 Franklin
# Street, Fifth Floor, Boston, MA 02110-1301 USA.

import sys
import struct
import math
import time
import argparse
import operator
import functools
import subprocess
try:
	from elftools.elf.elffile import ELFFile
	from elftools.elf.enums import *
	from elftools.elf.constants import *
except:
	print("Please install the python3 pyelftools")
	exit(2)

try:
	import serial
except:
	print("Please install the python3 pyserial package")
	exit(2)

def chunks(iterable, n=1):
   l = len(iterable)
   for ndx in range(0, l, n):
	   yield iterable[ndx:min(ndx+n, l)]

class ESPROM:

	# These are the currently known commands supported by the ROM bootloader
	ESP_FLASH_BEGIN = 0x02
	ESP_FLASH_DATA	= 0x03
	ESP_FLASH_END	= 0x04
	ESP_MEM_BEGIN	= 0x05
	ESP_MEM_END		= 0x06
	ESP_MEM_DATA	= 0x07
	ESP_SYNC		= 0x08
	ESP_WRITE_REG	= 0x09
	ESP_READ_REG	= 0x0a

	# Maximum block sizes for RAM and Flash writes, respectively.
	ESP_RAM_BLOCK	= 0x1800
	ESP_FLASH_BLOCK = 0x400

	# Default baudrate used by the ROM. Don't know if this can be changed.
	ESP_ROM_BAUD	= 115200

	# First byte of the application image
	ESP_IMAGE_MAGIC = 0xe9

	# Initial state for the checksum routine
	ESP_CHECKSUM_MAGIC = 0xef

	# Base address of the SPI mapping
	ESP_FLASH_BASE = 0x40200000

	def __init__(self, port=0):
		self._port = serial.Serial(port, self.ESP_ROM_BAUD)

	def read(self, length=1):
		""" Read bytes from the serial port while performing SLIP unescaping """
		def slip_read():
			c = self._port.read(1)[0]
			if c == 0xdb:
				try:
					return {0xdc: 0xc0,
							0xdd: 0xdb
						}[self._port.read(1)[0]]
				except KeyError:
					raise ValueError('Invalid SLIP escape sequence received from device')
			return c
		return bytes([slip_read() for _ in range(length)])

	def write(self, packet):
		""" Write bytes to the serial port while performing SLIP escaping """
		self._port.write(b'\xc0'+packet.replace(b'\xdb', b'\xdb\xdd').replace(b'\xc0', b'\xdb\xdc')+b'\xc0')

	@staticmethod
	def checksum(data, state=ESP_CHECKSUM_MAGIC):
		""" Calculate the XOR checksum of a blob, as it is defined by the ROM """
		return state ^ functools.reduce(operator.xor, data)

	def command(self, op=None, data=None, chk=0):
		""" Send a request and read the response """
		if op is not None:
			# Construct and send request
			pkt = struct.pack(b'<BBHI', 0x00, op, len(data), chk) + data
			self.write(pkt)

		# Read header of response and parse
		c = self._port.read(1)[0]
		if c != 0xc0:
			raise ValueError('Invalid head of packet: expected 0xc0, got {:#x}'.format(c))
		hdr = self.read(8)
		(resp, op_ret, len_ret, val) = struct.unpack(b'<BBHI', hdr)
		if resp != 0x01 or (op and op_ret != op):
			raise ValueError('Invalid response')

		# The variable-length body
		body = self.read(len_ret)

		# Terminating byte
		c = self._port.read(1)[0]
		if c != 0xc0:
			raise ValueError('Invalid end of packet: expected 0xc0, got {:#x}'.format(c))

		return val, body

	def simple_command(self, op=None, data=None, chk=0):
		rv, body = self.command(op, data, chk)
		if body != b'\0\0':
			raise ValueError('Invalid command response from device')
		return rv

	def sync(self):
		""" Perform a connection test """
		self.command(ESPROM.ESP_SYNC, b'\x07\x07\x12\x20'+32*b'\x55')
		for i in range(7):
			self.command()

	def connect(self):
		""" Try connecting repeatedly until successful, or giving up """
		self._port.timeout = 0.5
		for _i in range(10):
			try:
				self._port.flushInput()
				self._port.flushOutput()
				self.sync()
				self._port.timeout = 5
				return
			except:
				time.sleep(0.1)
		raise Exception('Failed to connect')

	def read_reg(self, addr):
		""" Read memory address in target """
		return self.simple_command(ESPROM.ESP_READ_REG, struct.pack(b'<I', addr))

	def write_reg(self, addr, value, mask, delay_us = 0):
		""" Write to memory address in target """
		return self.simple_command(ESPROM.ESP_WRITE_REG, struct.pack(b'<IIII', addr, value, mask, delay_us))

	def mem_begin(self, size, blocks, blocksize, offset):
		""" Start downloading an application image to RAM """
		return self.simple_command(ESPROM.ESP_MEM_BEGIN, struct.pack(b'<IIII', size, blocks, blocksize, offset))

	def mem_block(self, data, seq):
		""" Send a block of an image to RAM """
		return self.simple_command(ESPROM.ESP_MEM_DATA, struct.pack(b'<IIII', len(data), seq, 0, 0)+data, ESPROM.checksum(data))

	def mem_finish(self, entrypoint = 0):
		""" Leave download mode and run the application """
		return self.simple_command(ESPROM.ESP_MEM_END, struct.pack(b'<II', int(entrypoint == 0), entrypoint))

	def flash_begin(self, size, offset):
		""" Start downloading to Flash (performs an erase) """
		old_tmo, self._port.timeout = self._port.timeout, 10
		try:
			return self.simple_command(ESPROM.ESP_FLASH_BEGIN, struct.pack(b'<IIII', size, 0x200, 0x400, offset))
		finally:
			self._port.timeout = old_tmo

	def flash_block(self, data, seq):
		""" Write a block to flash """
		return self.simple_command(ESPROM.ESP_FLASH_DATA, struct.pack(b'<IIII', len(data), seq, 0, 0)+data, ESPROM.checksum(data))

	def flash_finish(self, reboot=False):
		""" Leave flash mode and run/reboot """
		pkt = struct.pack(b'<I', int(not reboot))
		rv, body = self.command(ESPROM.ESP_FLASH_END, pkt)
		if body not in (b'\0\0', b'\x01\x06'):
			raise Exception('Failed to leave Flash mode, expected one of b"\x01\x06", b"\x01\x06"; got ', body)

	def flash_image(self, offx, img, info=lambda *args:None):
		info('Erasing flash...')
		self.flash_begin(len(img), offx)
		for seq, chunk in enumerate(chunks(img, esp.ESP_FLASH_BLOCK)):
			info('\rWriting flash at {:#08x} ({:0f}%)...'.format(offx+seq*self.ESP_FLASH_BLOCK, seq*self.ESP_FLASH_BLOCK/len(img)*100))
			self.flash_block(chunk, seq)

	def write_memory_image(self, sections, entrypoint, info=lambda *args:None):
		for i, (addr, data) in enumerate(sections):
			info('Uploading section {}: {} bytes @{:#08x}'.format(i, len(data), addr))
			self.mem_begin(size, math.ceil(size/float(self.ESP_RAM_BLOCK)), self.ESP_RAM_BLOCK, addr)
			for seq, chunk in enumerate(chunks(data, self.ESP_RAM_BLOCK)):
				self.mem_block(chunk, seq)
			info('done!')
		info('All sections done, executing at {:#08x}'.format(entrypoint))
		self.mem_finish(entrypoint)

	def run(self, reboot=False):
		""" Run application code in flash """
		# Fake flash begin immediately followed by flash end
		self.flash_begin(0, 0)
		self.flash_finish(reboot)


class Image:
	def __init__(self, elffile=None, entrypoint=None, flash_base=ESPROM.ESP_FLASH_BASE):
		# Sections initially loaded by the bootloader into RAM
		self.loaded_sections = []
		# Sections directly memory-mapped from flash
		self.static_sections = []
		self.entrypoint = entrypoint
		self.flash_base = flash_base

		if elffile:
			with open(elffile, 'rb') as f:
				self.load_elf(f)

	def load_elf(self, f):
		elf = ELFFile(f)

		self.entrypoint = elf.header['e_entry']

		for sec in elf.iter_sections():
			if sec['sh_type'] == 'SHT_PROGBITS' and (sec['sh_flags'] & SH_FLAGS.SHF_ALLOC):
				addr = sec['sh_addr']
				data = sec.data()
				self.add_section(addr, data)

	def add_section(self, addr, data):
		if addr < self.flash_base:
			self.loaded_sections.append((addr, data))
		else:
			self.static_sections.append((addr, data))

	def loader_image(self):
		b = struct.pack(b'<BBBBI', ESPROM.ESP_IMAGE_MAGIC, len(self.loaded_sections), 0, 0, self.entrypoint)

		sechdr = lambda addr, size: struct.pack(b'<II', addr, size)
		for addr, data in self.loaded_sections:
			data = data + b'\0'*(3-(len(data)-1)%4)
			b += sechdr(addr, len(data)) + data

		checksum = ESPROM.checksum(b''.join(data for _1,data in self.loaded_sections))
		pad = 15-(len(b)%16)
		b += b'\0'*pad + bytes([checksum]) #FIXME 0xff-pad here?

		return b

	def static_image(self):
		b = bytes()

		self.static_sections.sort()
		base = self.static_sections[0][0] # take the lowest section's address as image base
		for addr, data in self.static_sections:
			b += b'\xff'*(addr-base-len(b))
			b += data

		return (base-self.flash_base), b

	def combined_image(self):
		limg = self.loader_image()
		sbase, simg = self.static_image()
		return limg + b'\xff'*(sbase-len(limg)) + simg

if __name__ == '__main__':
	_arg_auto_int = lambda x: int(x, 0)

	parser = argparse.ArgumentParser(description='ESP8266 ROM Bootloader Utility', prog='esptool')
	subparsers = parser.add_subparsers(dest='operation', help='Run esptool {command} -h for additional help')

	# load_ram
	parser_load_ram = subparsers.add_parser('load_ram', help='Download an image to RAM and execute')
	parser_load_ram.add_argument('port', help='Serial port where the ESP can be found')
	parser_load_ram.add_argument('filename', help='Firmware image')

	# dump_mem
	parser_dump_mem = subparsers.add_parser('dump_mem', help='Dump arbitrary memory to disk')
	parser_dump_mem.add_argument('port', help='Serial port where the ESP can be found')
	parser_dump_mem.add_argument('address', help='Base address', type=_arg_auto_int)
	parser_dump_mem.add_argument('size', help='Size of region to dump', type=_arg_auto_int)
	parser_dump_mem.add_argument('filename', help='Name of binary dump')

	# read_mem
	parser_read_mem = subparsers.add_parser('read_mem', help='Read arbitrary memory location')
	parser_read_mem.add_argument('port', help='Serial port where the ESP can be found')
	parser_read_mem.add_argument('address', help='Address to read', type=_arg_auto_int)

	# write_mem
	parser_write_mem = subparsers.add_parser('write_mem', help='Read-modify-write to arbitrary memory location')
	parser_write_mem.add_argument('port', help='Serial port where the ESP can be found')
	parser_write_mem.add_argument('address', help='Address to write', type=_arg_auto_int)
	parser_write_mem.add_argument('value', help='Value', type=_arg_auto_int)
	parser_write_mem.add_argument('mask', help='Mask of bits to write', type=_arg_auto_int)

	# write_flash
	parser_write_flash = subparsers.add_parser('write_flash', help='Write elf file to flash')
	parser_write_flash.add_argument('port', help='Serial port where the ESP can be found')
	parser_write_flash.add_argument('firmware', help='Firmware elf file')

	# make_image
	parser_make_image = subparsers.add_parser('make_image', help='Create a bootloader-compatible combined binary flash image from elf file')
	parser_make_image.add_argument('firmware', help='Firmware elf file')
	parser_make_image.add_argument('imageout', help='Output file where the image should be palced')

	# make_split_image
	parser_make_image = subparsers.add_parser('make_split_image', help='Create a bootloader-compatible split binary flash image from elf file')
	parser_make_image.add_argument('firmware', help='Firmware elf file')
	parser_make_image.add_argument('loaderout', help='Output file where the image for bootloaded sections should be palced')
	parser_make_image.add_argument('staticout', help='Output file where the image for statically mapped sections should be palced')

	# run
	parser_run = subparsers.add_parser('run', help='Run application code in flash')

	# image_info
	parser_image_info=subparsers.add_parser('image_info', help='Dump headers from an application image')
	parser_image_info.add_argument('filename', help='Image file to parse')

	args = parser.parse_args()


	# Create the ESPROM connection object, if needed
	if 'port' in args:
		esp = ESPROM(args.port)
		print('Connecting...', end='')
		esp.connect()
		print(' Connected.')

	# Do the actual work. Should probably be split into separate functions.
	if args.operation == 'load_ram':
		img = Image(args.firmware)
		esp.write_memory_image(img.loaded_sections + img.static_sections, img.entrypoint, info=print)

	elif args.operation == 'read_mem':
		print('@{:#08x}: {:#08x}'.format(args.address, esp.read_reg(args.address)))

	elif args.operation == 'write_mem':
		esp.write_reg(args.address, args.value, args.mask, 0)
		print('Wrote {:#08x} with mask {:#08x} to address {:#08x}'.format(args.value, args.mask, args.address))

	elif args.operation == 'dump_mem':
		with open(args.filename, 'wb') as f:
			for addr in range(args.address, args.address+args.size, 4):
				data = esp.read_reg(addr)
				f.write(struct.pack(b'<I', d))
				if f.tell() % 1024 == 0:
					print('{} bytes read ({:0f}%)'.format(f.tell(), f.tell()/args.size*100))

	elif args.operation == 'write_flash':
		img = Image(args.firmware)

		esp.flash_image(0x00000, img.loader_image(), info=print)
		base, data = img.static_image()
		esp.flash_image(base, data, info=print)
		esp.flash_finish(True)

	elif args.operation == 'make_image':
		img = Image(args.firmware)
		with open(args.imageout, 'wb') as out:
			out.write(img.combined_image())

	elif args.operation == 'make_split_image':
		img = Image(args.firmware)
		with open(args.loaderout, 'wb') as out:
			out.write(img.loader_image())
		with open(args.staticout, 'wb') as out:
			base, data = img.static_image()
			out.write(data)
			print('Static image flash offset: {:#x}'.format(base))

	elif args.operation == 'run':
		esp.run()

