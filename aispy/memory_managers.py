import multiprocessing as mp
from threading import Lock
from multiprocessing.shared_memory import SharedMemory

class SharedDeque:
	def  __init__(self, max_items, itemsize):
		self.max_items: mp.Value = mp.Value('I', max_items)
		self.itemsize: mp.Value = mp.Value('I', itemsize)
		self.lock: Lock = mp.Lock
		self.memory: SharedMemory = SharedMemory(create=True, size=max_items*itemsize)
		self.head: mp.Value = mp.Value('I', 0)
		self.tail: mp.Value = mp.Value('I', 0)
		self.num_items: mp.Value = mp.Value('I', 0)

	def pop(self):
		with self.lock:
			if self.num_items:
				self.num_items.value -= 1
				self.tail.value = (self.tail.value - 1 + self.max_items.value) % self.max_items.value
				return self.memory.buf[self.tail.value:self.tail.value + self.itemsize]
			else:
				raise IndexError('Pop from empty SharedDeque')

	def popleft(self):
		with self.lock:
			if self.num_items.value:
				self.num_items.value -= 1
				startloc = self.head.value
				endloc = startloc + self.itemsize
				self.head.value = (self.head.value + 1) % self.max_items.value
				return self.memory.buf[startloc:endloc]
			else:
				raise IndexError('Pop from empty SharedDeque')

	def append(self, item):
		with self.lock:
			#TODO Check item size
			self.memory.buf[self.tail.value:self.tail.value + self.itemsize] = item
			if self.num_items.value < self.max_items.value:
				self.num_items.value += 1
				self.tail.value = (self.tail.value + 1) % self.max_items.value
			else:
				self.tail.value = (self.tail.value + 1) % self.max_items.value
				self.head.value = self.tail.value
			return

	def appendleft(self, item):
		with self.lock:
			# TODO Check item size
			if self.num_items.value < self.max_items.value:
				self.num_items.value += 1
				self.head.value = (self.head.value - 1 + self.max_items.value) % self.max_items.value
			else:
				self.head.value = (self.head.value - 1 + self.max_items.value) % self.max_items.value
				self.tail.value = self.head.value
			self.memory.buf[self.head.value:self.head.value + self.itemsize] = item
			return