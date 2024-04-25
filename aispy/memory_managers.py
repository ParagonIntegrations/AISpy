import collections
import datetime
import multiprocessing as mp
from threading import RLock
from multiprocessing.shared_memory import SharedMemory

import numpy as np


class SharedFrameDeque:
	def  __init__(self, max_items, itemshape: tuple, datatype):
		self.max_items: int = int(max_items)
		self.itemshape: tuple = itemshape
		self.datatype:np.dtype = datatype
		self.itemsize: int = int(np.prod(itemshape) * datatype().itemsize)
		self.memsize: int = int(self.max_items * self.itemsize)
		self.lock: RLock = mp.RLock()
		self.memory: SharedMemory = SharedMemory(create=True, size=self.memsize)
		self.head: mp.Value = mp.Value('I', 0)
		self.tail: mp.Value = mp.Value('I', 0)
		self.num_items: mp.Value = mp.Value('I', 0)

	def pop(self) -> np.ndarray:
		with self.lock:
			if self.num_items:
				self.num_items.value -= 1
				self.tail.value = (self.tail.value - 1 + self.max_items) % self.max_items
				mbuf = bytearray(self.memory.buf[self.memtail:self.memtail + self.itemsize])
				return np.ndarray((self.itemshape), dtype=self.datatype, buffer=mbuf)
			else:
				raise IndexError('Pop from empty SharedDeque')

	def popleft(self) -> np.ndarray:
		with self.lock:
			if self.num_items.value:
				mbuf = bytearray(self.memory.buf[self.memhead:self.memhead + self.itemsize])
				self.num_items.value -= 1
				self.head.value = (self.head.value + 1) % self.max_items
				return np.ndarray((self.itemshape), dtype=self.datatype, buffer=mbuf, )
			else:
				raise IndexError('Pop from empty SharedDeque')

	def append(self, item: np.ndarray):
		with self.lock:
			if item.dtype != self.datatype:
				raise ValueError('Wrong item datatype')
			if item.shape != self.itemshape:
				raise ValueError('Wrong item shape')
			self.memory.buf[self.memtail:self.memtail + self.itemsize] = item.tobytes()
			if self.num_items.value < self.max_items:
				self.num_items.value += 1
				self.tail.value = (self.tail.value + 1) % self.max_items
			else:
				self.tail.value = (self.tail.value + 1) % self.max_items
				self.head.value = self.tail.value
			return

	def appendleft(self, item: np.ndarray):
		with self.lock:
			if item.dtype != self.datatype:
				raise ValueError('Wrong item datatype')
			if item.shape != self.itemshape:
				raise ValueError('Wrong item shape')
			if self.num_items.value < self.max_items:
				self.num_items.value += 1
				self.head.value = (self.head.value - 1 + self.max_items) % self.max_items
			else:
				self.head.value = (self.head.value - 1 + self.max_items) % self.max_items
				self.tail.value = self.head.value
			self.memory.buf[self.memhead:self.memhead + self.itemsize] = item
			return

	def getbyarrayindex(self, arrayindex) -> np.ndarray:
		# No guarantee that the item still exists
		with self.lock:
			if arrayindex > self.max_items:
				IndexError(f'Cannot access item SharedDeque is only {self.max_items} long')
			arrayindex = (arrayindex + self.max_items) % self.max_items
			mbuf = bytearray(self.memory.buf[self.memaddr(arrayindex):self.memaddr(arrayindex + 1)])
			return np.ndarray(self.itemshape, dtype=self.datatype, buffer=mbuf)

	def getwithindex(self, key) -> tuple[np.ndarray, int]:
		return self.__getitem__(key, True)

	@property
	def memhead(self):
		return self.head.value * self.itemsize

	@property
	def memtail(self):
		return self.tail.value * self.itemsize

	def memaddr(self, index):
		return index * self.itemsize

	def __getitem__(self, key, withindex = False) -> np.ndarray|tuple[np.ndarray, int]|list[np.ndarray]|list[tuple[np.ndarray, int]]:
		with self.lock:
			if isinstance(key, slice):
				start, stop, step = key.indices(self.num_items.value)
				return [self.__getitem__(x, withindex) for x in range(start, stop, step)]
			elif isinstance(key, int):
				if key > self.num_items.value:
					IndexError(f'Cannot access item {key}, only {self.num_items.value} in SharedDeque')
				if key >= 0:
					keyindex = (self.head.value + key) % self.max_items
					mbuf = bytearray(self.memory.buf[self.memaddr(keyindex):self.memaddr(keyindex + 1)])
				else:
					keyindex = (self.tail.value + self.max_items + key) % self.max_items
					mbuf = bytearray(self.memory.buf[self.memaddr(keyindex):self.memaddr(keyindex + 1)])
			else:
				raise TypeError('Invalid argument type')

			if withindex:
				return np.ndarray(self.itemshape, dtype=self.datatype, buffer=mbuf), keyindex
			return np.ndarray(self.itemshape, dtype=self.datatype, buffer=mbuf)

	def __len__(self):
		with self.lock:
			return self.num_items.value

	def __del__(self):
		self.memory.close()
		self.memory.unlink()

class MotionDetectorSharedMemory:
	def __init__(self, max_items):
		self.max_items = max_items
		self.frameid = mp.Value('I', 0)
		self.sharedmemory = 1



if __name__ == '__main__':
	# shape = (1, 1)
	# type = np.uint8
	# deq = SharedFrameDeque(10, shape, type)
	# for i in range(14):
	# 	a = np.full(shape=shape, fill_value=i, dtype=type)
	# 	deq.append(a)
	# deq.popleft()
	# deq.popleft()
	# deq.popleft()
	#
	# print(deq[1::3])

	# Test speed
	shape = (1080,1920,3)
	type = np.uint8
	img = np.full(shape=(1080,1920,3), fill_value=128, dtype=type)
	shareddeque_size = int(5*15)
	deq = SharedFrameDeque(shareddeque_size, shape, type)
	# deq = collections.deque(maxlen=shareddeque_size)

	# Test append speed
	start = datetime.datetime.now()
	for i in range(1000):
		deq.append(img)
	end = datetime.datetime.now()
	speed = (end - start).total_seconds()
	print(f'Append speed is {speed:0.2f}ms')

	# Test get last item speed
	start = datetime.datetime.now()
	for i in range(1000):
		img = deq[-1]
	end = datetime.datetime.now()
	speed = (end - start).total_seconds()
	print(f'Get speed is {speed:0.2f}ms')

	# Test get item by arrayindex speed
	start = datetime.datetime.now()
	for i in range(1000):
		img = deq.getbyarrayindex(-1)
	end = datetime.datetime.now()
	speed = (end - start).total_seconds()
	print(f'Get by array index speed is {speed:0.2f}ms')

	# Test pop speed
	start = datetime.datetime.now()
	for i in range(shareddeque_size):
		img = deq.pop()
	end = datetime.datetime.now()
	speed = (end - start).total_seconds()/shareddeque_size*1000
	print(f'Pop speed is {speed:0.2f}ms')