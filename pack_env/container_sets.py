'''
Configuration for all containers simultaneously; (check CONTAINERS_CONFIG in root directory's containers_info.py)
	- current environment state info about container occupancy & item placement so far
	- name of container holding various items
'''
import numpy as np
from functools import reduce
import copy, time, sys
from joblib import Parallel, delayed, parallel_backend

from .box import Box
from .container import Container

sys.path.append("../")
import config

class ContainerSets():
	def __init__(self, box_list):
		'''
		Initializes all containers available for packing
		Also intializes all items/boxes to be packed since all the items known beforehand for our Offline BPP case
		box_list: customer order during prediction & artifical box list for training generated by box_seq_generator
		'''
		self.box_list = box_list
		# self.init_container_ids_list = init_container_ids_list

		self.num_containers  = config.num_containers
		self.container_names = [config.CONTAINERS_CONFIG["container_details"][container_id]["name"] 
									for container_id in range(config.num_containers)]
		
		self.allow_rotations = config.allow_rotations
		self.num_rotations   = len(config.allow_rotations)
		self.num_items  = config.num_items
		self.x_poss = config.x_placement_poss
		self.y_poss = config.y_placement_poss
		self.num_X = len(self.x_poss)
		self.num_Y = len(self.y_poss) 	
		self.max_X = config.max_X
		self.max_Y = config.max_Y
		self.max_Z = config.max_Z
		self.max_W = config.max_W

		# initialize all containers
		self.containers = []
		self.containers_hwv_maps = []
		self.containers_invalid_XY = []
		self.combined_hwv_map = None
		self._init_containers()

		# keep track if any container is used multiple times, if yes, rename them separately for adjusting container counts later
		self.used_containers = []
		self.container_use_times = {container_id:{"orig_name":self.container_names[container_id], "used_times":1} for container_id in range(self.num_containers)}
		self.container_use_names = {container_id:{"used_name": None, "packed_boxes":[]} for container_id in range(self.num_containers)}
		self.suitable_container_id = None

		# containers & boxes placed in them
		self.container_placedBox_lookUp  = {}
		self.container_placedBox_lookUps = {}

		self.packed_boxes   = []		
		self.current_box_id = 0


	def reset(self):
		self.containers = []
		self.containers_invalid_XY = []
		self.combined_hwv_map = None
		self._init_containers()

		# keep track if any container is used multiple times, if yes, rename them separately for adjusting container counts later
		self.used_containers = []
		self.container_use_times = {container_id:{"orig_name":self.container_names[container_id], "used_times":1} for container_id in range(self.num_containers)}
		self.container_use_names = {container_id:{"used_name": None, "packed_boxes":[]} for container_id in range(self.num_containers)}
		
		self.suitable_container_id = None

		# containers & boxes placed in them
		self.container_placedBox_lookUp  = {}
		self.container_placedBox_lookUps = {}

		self.packed_boxes   = []		
		self.current_box_id = 0

	def _init_containers(self):
		combined_hwv_map  = []
		for container_id in range(self.num_containers):
			
			container_name =  config.CONTAINERS_CONFIG["container_details"][container_id]["name"]   
			dx, dy, dz =  [config.CONTAINERS_CONFIG["container_details"][container_id][dim] for dim in ["X", "Y", "Z"]]
			max_wt     = config.CONTAINERS_CONFIG["container_details"][container_id]["max_weight"]
			container  = Container(dx, dy, dz, max_wt, self.max_X, self.max_Y, self.max_Z, self.max_W, container_name)
			container.reset()
			
			self.containers.append(container)
			self.containers_hwv_maps.append(container.get_hwv_map())
			combined_hwv_map.append(container.get_hwv_map())

			self.containers_invalid_XY.append({"x": list(range(dx, self.max_X)), "y": list(range(dy, self.max_Y))})
		
		self.combined_hwv_map = np.concatenate(combined_hwv_map, axis=0)


	def get_valid_mask(self, box, use_container_ids, rotations):
		mask   = np.zeros((self.num_containers, self.num_rotations, self.max_X, self.max_Y), dtype=np.int8)
		box_dims = [box.dx, box.dy, box.dz]
		box_dims.sort(reverse=True)

		# if box_id == 0:
		# 	sum_cntr_mask = 1
		# elif 1<= box_id <= len(self.box_list) // 3:
		# 	sum_cntr_mask = 10
		# else:
		# 	sum_cntr_mask = 50
		sum_cntr_mask = 1000

		for container_id in use_container_ids:
			container      = self.containers[container_id]
			container_dims = [container.dx, container.dy, container.dz]
			container_dims.sort(reverse=True)

			# check any dim violation
			dim_condn = (sum([container_dims[k] < box_dims[k] for k in range(3)]) > 0)
			wt_condn  = container.free_wt < box.wt
			vol_condn = container.free_vol < box.vol()

			if dim_condn or wt_condn or vol_condn:continue

			for rotation in rotations:
				start = time.time()
				b = copy.deepcopy(box)
				# print("dc", time.time() - start)
				if rotation > 0:b.rotate(rotation)

				if (b.dx > container.dx) or (b.dy > container.dy) or (b.dz > container.dz):continue

				for y in range(container.dy - b.dy + 1):
					# if rotation == 2 and y <= int(container.dy*.8): break
					if y in self.containers_invalid_XY[container_id]["y"]:break

					for x in range(container.dx - b.dx + 1):
						# if rotation == 3 and x <= int(container.dx*.8): break
						if x in self.containers_invalid_XY[container_id]["x"]:break

						if (np.sum(mask) >= 1000) or (np.sum(mask[container_id, ...]) >= sum_cntr_mask):break

						valid_placement = container.check_box_placement_valid(b, (x, y))

						if valid_placement >= 0:
							mask[container_id, rotation, x, y] = 1
					
					if (np.sum(mask) >= 1000) or (np.sum(mask[container_id, ...]) >= sum_cntr_mask):break
				
			# 	if (np.sum(mask) >= 100) or (np.sum(mask[container_id, ...]) >= sum_cntr_mask):break
			# if (np.sum(mask) >= 100) or (np.sum(mask[container_id, ...]) >= sum_cntr_mask):break

		return mask


	def drop_box(self, current_box_id, container_id, box, pos, actions, used_containers, check_print=False, print_mcts_sim=False):
		container = self.containers[container_id]
		succeded, box_packed = container.drop_box(box, pos, check_print)

		if succeded:
			self.packed_boxes.append(box_packed)

			placed_container_name = self.container_use_times[container_id]["orig_name"] + "(" + \
										str(self.container_use_times[container_id]["used_times"]) + ")"

			if check_print:
				print("\n\tDROPPING", box.name, "in=>", placed_container_name, container_id)
			
			if placed_container_name not in self.container_placedBox_lookUp.keys():
				if check_print:
					print("\tNAME NOT IN", placed_container_name, container_id)

				self.container_placedBox_lookUp.update({placed_container_name:{"container_id":container_id, "packed_boxes":[]}})
				self.container_placedBox_lookUps.update({placed_container_name:{"container_id":container_id, "packed_boxes":[]}})

			box_packed.pack_cntr_id = container_id
			box_packed.pack_cntr_name = placed_container_name
			box_packed.pack_cntr_size = (container.dx, container.dy, container.dz)
			box_packed.pack_rot = actions[1]
			box_packed.z -= (self.max_Z - container.dz)

			self.container_placedBox_lookUp[placed_container_name]["packed_boxes"].append(box_packed)

			self.current_box_id += 1

		return succeded, box_packed



	def replace_containers(self, suitable_container_id, check_print=False):
		'''
		box: incoming box which couldn't be placed in any pre-existing box
		'''

		# if none of existing containers can hold current box
		placed_container_name = self.container_use_times[suitable_container_id]["orig_name"] + "(" + \
									str(self.container_use_times[suitable_container_id]["used_times"]) + ")"				
		
		if placed_container_name in self.container_placedBox_lookUp.keys():
			self.container_use_times[suitable_container_id]["used_times"] += 1

		# if check_print:
		# 	print("\tcontainer{}: hmap before reset:{}".format(suitable_container_id, self.containers[suitable_container_id].height_map))
		self.containers[suitable_container_id].reset()
		# if check_print:
		# 	print("\tcontainer{}: hmap after reset:{}".format(suitable_container_id, self.containers[suitable_container_id].height_map))		
		
		self.suitable_container_id = suitable_container_id


	def get_all_containers_hwv_map(self):
		combined_hwv_map = []		
		for container_id in range(self.num_containers):
			combined_hwv_map.append(self.containers[container_id].get_hwv_map())

		combined_hwv_map = np.concatenate(combined_hwv_map, axis=0)
		return combined_hwv_map


	def update_combined_hwv_map(self):
		combined_hwv_map = []
		for container_id in range(self.num_containers):
			container = self.containers[container_id]
			combined_hwv_map.append(container.get_hwv_map())
		self.combined_hwv_map = np.concatenate(combined_hwv_map, axis=0)


	def reset_list(self, list_, current_id, replace_id):
		current_item = list_.pop(current_id)
		replace_item = list_.pop(replace_id - 1)
		final_list = list_[:current_id] + [replace_item, current_item] + list_[current_id:]
		return final_list

	def reset_box_list_mask(self, current_box_id, replace_box_id):
		self.box_list = self.reset_list(copy.deepcopy(self.box_list), current_box_id, replace_box_id)
