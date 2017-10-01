__author__ = 'maartenbreddels'
import os
import mmap
import itertools
import functools
import collections
import logging
import numpy as np
import numpy.ma
import vaex
import astropy.table
import astropy.units
from vaex.utils import ensure_string
import astropy.io.fits as fits
import re
import six

from vaex.dataset import DatasetLocal, DatasetArrays
logger = logging.getLogger("vaex.file")
import vaex.dataset
import vaex.file
dataset_type_map = {}

# h5py doesn't want to build at readthedocs
on_rtd = os.environ.get('READTHEDOCS', None) == 'True'
try:
	import h5py
except:
	if not on_rtd:
		raise

from vaex.expression import Expression
from vaex.dataset_mmap import DatasetMemoryMapped

def _try_unit(unit):
	try:
		unit = astropy.units.Unit(str(unit))
		if not isinstance(unit, astropy.units.UnrecognizedUnit):
			return unit
	except:
		#logger.exception("could not parse unit: %r", unit)
		pass
	try:
		unit_mangle = re.match(".*\[(.*)\]", str(unit)).groups()[0]
		unit = astropy.units.Unit(unit_mangle)
	except:
		pass#logger.exception("could not parse unit: %r", unit)
	if isinstance(unit, six.string_types):
		return None
	elif isinstance(unit, astropy.units.UnrecognizedUnit):
		return None
	else:
		return unit

class Hdf5MemoryMapped(DatasetMemoryMapped):
	"""Implements the vaex hdf5 file format"""
	def __init__(self, filename, write=False):
		super(Hdf5MemoryMapped, self).__init__(filename, write=write)
		self.h5file = h5py.File(self.filename, "r+" if write else "r")
		self.h5table_root_name = None
		self._version = 1
		try:
			self._load()
		finally:
			self.h5file.close()

	def write_meta(self):
		"""ucds, descriptions and units are written as attributes in the hdf5 file, instead of a seperate file as
		 the default :func:`Dataset.write_meta`.
		 """
		with h5py.File(self.filename, "r+") as h5file_output:
			h5table_root = h5file_output[self.h5table_root_name]
			if self.description is not None:
				h5table_root.attrs["description"] = self.description
			h5columns = h5table_root if self._version == 1 else h5table_root['columns']
			for column_name in self.columns.keys():
				h5dataset = h5columns[column_name]
				for name, values in [("ucd", self.ucds), ("unit", self.units), ("description", self.descriptions)]:
					if column_name in values:
						value = ensure_string(values[column_name], cast=True)
						h5dataset.attrs[name] = value
					else:
						if name in h5columns.attrs:
							del h5dataset.attrs[name]
	@classmethod
	def create(cls, path, N, column_names, dtypes=None, write=True):
		"""Create a new (empty) hdf5 file with columns given by column names, of length N

		Optionally, numpy dtypes can be passed, default is floats
		"""

		dtypes = dtypes or [np.float] * len(column_names)

		if N == 0:
			raise ValueError("Cannot export empty table")
		with h5py.File(path, "w") as h5file_output:
			h5data_output = h5file_output.require_group("data")
			for column_name, dtype in zip(column_names, dtypes):
				shape = (N,)
				print(dtype)
				if dtype.type == np.datetime64:
					array = h5file_output.require_dataset("/data/%s" % column_name, shape=shape, dtype=np.int64)
					array.attrs["dtype"] = dtype.name
				else:
					array = h5file_output.require_dataset("/data/%s" % column_name, shape=shape, dtype=dtype)
				array[0] = array[0] # make sure the array really exists
		return Hdf5MemoryMapped(path, write=write)

	@classmethod
	def can_open(cls, path, *args, **kwargs):
		h5file = None
		try:
			with open(path, "rb") as f:
				signature = f.read(4)
				hdf5file = signature == b"\x89\x48\x44\x46"
		except:
			logger.error("could not read 4 bytes from %r", path)
			return
		if hdf5file:
			try:
				h5file = h5py.File(path, "r")
			except:
				logger.exception("could not open file as hdf5")
				return False
			if h5file is not None:
				with h5file:
					return ("data" in h5file) or ("columns" in h5file) or ("table" in h5file)
			else:
				logger.debug("file %s has no data or columns group" % path)
		return False


	@classmethod
	def get_options(cls, path):
		return []

	@classmethod
	def option_to_args(cls, option):
		return []

	def _load(self):
		if "data" in self.h5file:
			self._load_columns(self.h5file["/data"])
			self.h5table_root_name = "/data"
		if "table" in self.h5file:
			self._version = 2
			self._load_columns(self.h5file["/table"])
			self.h5table_root_name = "/table"
		# TODO: shall we rename it vaex... ?
		# if "vaex" in self.h5file:
		#	self.load_columns(self.h5file["/vaex"])
		#	h5table_root = "/vaex"
		if "columns" in self.h5file:
			self._load_columns(self.h5file["/columns"])
			self.h5table_root_name = "/columns"
		if "properties" in self.h5file:
			self._load_variables(self.h5file["/properties"]) # old name, kept for portability
		if "variables" in self.h5file:
			self._load_variables(self.h5file["/variables"])
		if "axes" in self.h5file:
			self._load_axes(self.h5file["/axes"])
		self.update_meta()
		self.update_virtual_meta()
		self.selections_favorite_load()

	#def
	def _load_axes(self, axes_data):
		for name in axes_data:
			axis = axes_data[name]
			logger.debug("loading axis %r" % name)
			offset = axis.id.get_offset()
			shape = axis.shape
			assert len(shape) == 1 # ony 1d axes
			#print name, offset, len(axis), axis.dtype
			self.addAxis(name, offset=offset, length=len(axis), dtype=axis.dtype)
			#self.axis_names.append(axes_data)
			#self.axes[name] = np.array(axes_data[name])

	def _load_variables(self, h5variables):
		for key, value in list(h5variables.attrs.items()):
			self.variables[key] = value


	def _load_columns(self, h5data, first=[]):
		#print h5data
		# make sure x y x etc are first

		finished = set()
		if "description" in h5data.attrs:
			self.description = ensure_string(h5data.attrs["description"])
		# hdf5, or h5py doesn't keep the order of columns, so manually track that, also enables reordering later
		h5columns = h5data if self._version == 1 else h5data['columns']
		if "column_order" in h5columns.attrs:
			column_order = ensure_string(h5columns.attrs["column_order"]).split(",")
		else:
			column_order = []
		for name in list(h5columns):
			if name not in column_order:
				column_order.append(name)
		for column_name in column_order:
			if column_name in h5columns and column_name not in finished:
				column = h5columns[column_name]
				if "ucd" in column.attrs:
					self.ucds[column_name] = ensure_string(column.attrs["ucd"])
				if "description" in column.attrs:
					self.descriptions[column_name] = ensure_string(column.attrs["description"])
				if "unit" in column.attrs:
					try:
						unitname = ensure_string(column.attrs["unit"])
						if unitname and unitname != "None":
							self.units[column_name] = _try_unit(unitname)
					except:
						logger.exception("error parsing unit: %s", column.attrs["unit"])
				if "units" in column.attrs: # Amuse case
					unitname = ensure_string(column.attrs["units"])
					logger.debug("amuse unit: %s", unitname)
					if unitname == "(0.01 * system.get('S.I.').base('length'))":
						self.units[column_name] = astropy.units.Unit("cm")
					if unitname == "((0.01 * system.get('S.I.').base('length')) * (system.get('S.I.').base('time')**-1))":
						self.units[column_name] = astropy.units.Unit("cm/s")
					if unitname == "(0.001 * system.get('S.I.').base('mass'))":
						self.units[column_name] = astropy.units.Unit("gram")

					if unitname == "system.get('S.I.').base('length')":
						self.units[column_name] = astropy.units.Unit("m")
					if unitname == "(system.get('S.I.').base('length') * (system.get('S.I.').base('time')**-1))":
						self.units[column_name] = astropy.units.Unit("m/s")
					if unitname == "system.get('S.I.').base('mass')":
						self.units[column_name] = astropy.units.Unit("kg")
				data = column if self._version == 1 else column['data']
				if hasattr(data, "dtype"):
					#print column, column.shape
					offset = data.id.get_offset()
					if offset is None:
						raise Exception("columns doesn't really exist in hdf5 file")
					shape = data.shape
					if True: #len(shape) == 1:
						dtype = data.dtype
						if "dtype" in data.attrs:
							dtype = data.attrs["dtype"]
						logger.debug("adding column %r with dtype %r", column_name, dtype)
						self.addColumn(column_name, offset, len(data), dtype=dtype)
						if self._version > 1 and 'mask' in column:
							mask = column['mask']
							offset = mask.id.get_offset()
							self.addColumn("temp_mask", offset, len(data), dtype=mask.dtype)
							mask_array = self.columns['temp_mask']
							del self.columns['temp_mask']
							self.column_names.remove('temp_mask')
							ar = self.columns[column_name] = np.ma.array(self.columns[column_name], mask=mask_array, shrink=False)
							assert ar.mask is mask_array, "masked array was copied"

					else:

						#transposed = self._length is None or shape[0] == self._length
						transposed = shape[1] < shape[0]
						self.addRank1(column_name, offset, shape[1], length1=shape[0], dtype=data.dtype, stride=1, stride1=1, transposed=transposed)
						#if len(shape[0]) == self._length:
						#	self.addRank1(column_name, offset, shape[1], length1=shape[0], dtype=column.dtype, stride=1, stride1=1)
						#self.addColumn(column_name+"_0", offset, shape[1], dtype=column.dtype)
						#self.addColumn(column_name+"_last", offset+(shape[0]-1)*shape[1]*column.dtype.itemsize, shape[1], dtype=column.dtype)
						#self.addRank1(name, offset+8*i, length=self.numberParticles+1, length1=self.numberTimes-1, dtype=np.float64, stride=stride, stride1=1, filename=filename_extra)
			finished.add(column_name)

	def close(self):
		super(Hdf5MemoryMapped, self).close()
		self.h5file.close()

	def __expose_array(self, hdf5path, column_name):
		array = self.h5file[hdf5path]
		array[0] = array[0] # without this, get_offset returns None, probably the array isn't really created
		offset = array.id.get_offset()
		self.remap()
		self.addColumn(column_name, offset, len(array), dtype=array.dtype)

	def __add_column(self, column_name, dtype=np.float64, length=None):
		array = self.h5data.create_dataset(column_name, shape=(self._length if length is None else length,), dtype=dtype)
		array[0] = array[0] # see above
		offset = array.id.get_offset()
		self.h5file.flush()
		self.remap()
		self.addColumn(column_name, offset, len(array), dtype=array.dtype)

dataset_type_map["h5vaex"] = Hdf5MemoryMapped

class AmuseHdf5MemoryMapped(Hdf5MemoryMapped):
	"""Implements reading Amuse hdf5 files `amusecode.org <http://amusecode.org/>`_"""
	def __init__(self, filename, write=False):
		super(AmuseHdf5MemoryMapped, self).__init__(filename, write=write)

	@classmethod
	def can_open(cls, path, *args, **kwargs):
		h5file = None
		try:
			h5file = h5py.File(path, "r")
		except:
			return False
		if h5file is not None:
			with h5file:
				return ("particles" in h5file)# or ("columns" in h5file)
		return False

	def _load(self):
		particles = self.h5file["/particles"]
		for group_name in particles:
			#import pdb
			#pdb.set_trace()
			group = particles[group_name]
			self._load_columns(group["attributes"])

			column_name = "keys"
			column = group[column_name]
			offset = column.id.get_offset()
			self.addColumn(column_name, offset, len(column), dtype=column.dtype)
		self.update_meta()
		self.update_virtual_meta()
		self.selections_favorite_load()

dataset_type_map["amuse"] = AmuseHdf5MemoryMapped


gadget_particle_names = "gas halo disk bulge stars dm".split()

class Hdf5MemoryMappedGadget(DatasetMemoryMapped):
	"""Implements reading `Gadget2 <http://wwwmpa.mpa-garching.mpg.de/gadget/>`_ hdf5 files """
	def __init__(self, filename, particle_name=None, particle_type=None):
		if "#" in filename:
			filename, index = filename.split("#")
			index = int(index)
			particle_type = index
			particle_name = gadget_particle_names[particle_type]
		elif particle_type is not None:
			self.particle_name = gadget_particle_names[self.particle_type]
			self.particle_type = particle_type
		elif particle_name is not None:
			if particle_name.lower() in gadget_particle_names:
				self.particle_type = gadget_particle_names.index(particle_name.lower())
				self.particle_name = particle_name.lower()
			else:
				raise ValueError("particle name not supported: %r, expected one of %r" % (particle_name, " ".join(gadget_particle_names)))
		else:
			raise Exception("expected particle type or name as argument, or #<nr> behind filename")
		super(Hdf5MemoryMappedGadget, self).__init__(filename)
		self.particle_type = particle_type
		self.particle_name = particle_name
		self.name = self.name + "-" + self.particle_name
		h5file = h5py.File(self.filename, 'r')
		#for i in range(1,4):
		key = "/PartType%d" % self.particle_type
		if key not in h5file:
			raise KeyError("%s does not exist" % key)
		particles = h5file[key]
		for name in list(particles.keys()):
			#name = "/PartType%d/Coordinates" % i
			data = particles[name]
			if isinstance(data, h5py.highlevel.Dataset): #array.shape
				array = data
				shape = array.shape
				if len(shape) == 1:
					offset = array.id.get_offset()
					if offset is not None:
						self.addColumn(name, offset, data.shape[0], dtype=data.dtype)
				else:
					if name == "Coordinates":
						offset = data.id.get_offset()
						if offset is None:
							print((name, "is not of continuous layout?"))
							sys.exit(0)
						bytesize = data.dtype.itemsize
						self.addColumn("x", offset, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("y", offset+bytesize, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("z", offset+bytesize*2, data.shape[0], dtype=data.dtype, stride=3)
					elif name == "Velocity":
						offset = data.id.get_offset()
						self.addColumn("vx", offset, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("vy", offset+bytesize, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("vz", offset+bytesize*2, data.shape[0], dtype=data.dtype, stride=3)
					elif name == "Velocities":
						offset = data.id.get_offset()
						self.addColumn("vx", offset, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("vy", offset+bytesize, data.shape[0], dtype=data.dtype, stride=3)
						self.addColumn("vz", offset+bytesize*2, data.shape[0], dtype=data.dtype, stride=3)
					else:
						logger.error("unsupported column: %r of shape %r" % (name, array.shape))
		if "Header" in h5file:
			for name in "Redshift Time_GYR".split():
				if name in h5file["Header"].attrs:
					value = h5file["Header"].attrs[name].decode("utf-8")
					logger.debug("property[{name!r}] = {value}".format(**locals()))
					self.variables[name] = value
					#self.property_names.append(name)

		name = "particle_type"
		value = particle_type
		logger.debug("property[{name}] = {value}".format(**locals()))
		self.variables[name] = value
		#self.property_names.append(name)

	@classmethod
	def can_open(cls, path, *args, **kwargs):
		if len(args) == 2:
			particleName = args[0]
			particleType = args[1]
		elif "particle_name" in kwargs:
			particle_type = gadget_particle_names.index(kwargs["particle_name"].lower())
		elif "particle_type" in kwargs:
			particle_type = kwargs["particle_type"]
		elif "#" in path:
			filename, index = path.split("#")
			particle_type = gadget_particle_names[index]
		else:
			return False
		h5file = None
		try:
			h5file = h5py.File(path, "r")
		except:
			return False
		has_particles = False
		#for i in range(1,6):
		key = "/PartType%d" % particle_type
		exists = key in h5file
		h5file.close()
		return exists

		#has_particles = has_particles or (key in h5file)
		#return has_particles


	@classmethod
	def get_options(cls, path):
		return []

	@classmethod
	def option_to_args(cls, option):
		return []


dataset_type_map["gadget-hdf5"] = Hdf5MemoryMappedGadget

class MemoryMappedGadget(DatasetMemoryMapped):
	def __init__(self, filename):
		super(MemoryMappedGadget, self).__init__(filename)
		#h5file = h5py.File(self.filename)
		import vaex.file.gadget
		length, posoffset, veloffset, header = vaex.file.gadget.getinfo(filename)
		self.addColumn("x", posoffset, length, dtype=np.float32, stride=3)
		self.addColumn("y", posoffset+4, length, dtype=np.float32, stride=3)
		self.addColumn("z", posoffset+8, length, dtype=np.float32, stride=3)

		self.addColumn("vx", veloffset, length, dtype=np.float32, stride=3)
		self.addColumn("vy", veloffset+4, length, dtype=np.float32, stride=3)
		self.addColumn("vz", veloffset+8, length, dtype=np.float32, stride=3)
dataset_type_map["gadget-plain"] = MemoryMappedGadget

