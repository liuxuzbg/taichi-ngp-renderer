from multiprocessing import shared_memory
import os
import numpy as np
import argparse
from matplotlib import pyplot as plt
from scipy.spatial.transform import Rotation as R
import time
import taichi as ti
from taichi.math import uvec3, vec3, vec2
import wget
import cv2
import platform
from camera import OrbitCamera

from typing import Tuple

def depth2img(depth):
    depth = (depth-depth.min())/(depth.max()-depth.min())
    depth_img = cv2.applyColorMap((depth*255).astype(np.uint8),
                                  cv2.COLORMAP_TURBO)

    return depth_img

arch = ti.cuda if ti._lib.core.with_cuda() else ti.vulkan
# arch = ti.vulkan

if platform.system() == 'Darwin':
    block_dim = 64
else:
    block_dim = 128

data_type = ti.f16
np_type = np.float16
tf_vec3 = ti.types.vector(3, dtype=data_type)
tf_vec8 = ti.types.vector(8, dtype=data_type)
tf_vec16 = ti.types.vector(16, dtype=data_type)
tf_vec32 = ti.types.vector(32, dtype=data_type)
tf_vec1 = ti.types.vector(1, dtype=data_type)
tf_vec2 = ti.types.vector(2, dtype=data_type)
tf_mat1x3 = ti.types.matrix(1, 3, dtype=data_type)
tf_index_temp = ti.types.vector(8, dtype=ti.i32)

MAX_SAMPLES = 1024
NEAR_DISTANCE = 0.01

SQRT3 = 1.7320508075688772
SQRT3_MAX_SAMPLES = SQRT3/1024
SQRT3_2 = 1.7320508075688772*2
PRETRAINED_MODEL_URL = 'https://github.com/Linyou/taichi-ngp-renderer/releases/download/v0.2/{}.npy'

#<----------------- hash table util code ----------------->
@ti.func
def calc_dt(t, exp_step_factor, grid_size, scale):
    return data_type(ti.math.clamp(t*exp_step_factor, SQRT3_MAX_SAMPLES, SQRT3_2*scale/grid_size))

@ti.func
def __expand_bits(v):
    v = (v * ti.uint32(0x00010001)) & ti.uint32(0xFF0000FF)
    v = (v * ti.uint32(0x00000101)) & ti.uint32(0x0F00F00F)
    v = (v * ti.uint32(0x00000011)) & ti.uint32(0xC30C30C3)
    v = (v * ti.uint32(0x00000005)) & ti.uint32(0x49249249)
    return v


@ti.func
def __morton3D(xyz):
    xyz = __expand_bits(xyz)
    return xyz[0] | (xyz[1] << 1) | (xyz[2] << 2)

@ti.func
def fast_hash(pos_grid_local):
    result = ti.uint32(0)
    primes = uvec3(ti.uint32(1), ti.uint32(2654435761), ti.uint32(805459861))
    for i in ti.static(range(3)):
        result ^= ti.uint32(pos_grid_local[i]) * primes[i]
    return result

@ti.func
def under_hash(pos_grid_local, resolution):
    result = ti.uint32(0)
    stride = ti.uint32(1)
    for i in ti.static(range(3)):
        result += ti.uint32(pos_grid_local[i] * stride)
        stride *= resolution
    return result

@ti.func
def grid_pos2hash_index(indicator, pos_grid_local, resolution, map_size):
    hash_result = ti.uint32(0)
    if indicator == 1:
        hash_result = under_hash(pos_grid_local, resolution)
    else:
        hash_result = fast_hash(pos_grid_local)

    return hash_result % map_size

@ti.func
def frexp_bit(x):
    exponent = 0
    if x != 0.0:
        # frac = ti.abs(x)
        bits = ti.bit_cast(x, ti.u32)
        exponent = ti.i32((bits & ti.u32(0x7f800000)) >> 23) - 127
        # exponent = (ti.i32(bits & ti.u32(0x7f800000)) >> 23) - 127
        bits &= ti.u32(0x7fffff)
        bits |= ti.u32(0x3f800000)
        frac = ti.bit_cast(bits, ti.f32)
        if frac < 0.5:
            exponent -= 1
        elif frac > 1.0:
            exponent += 1
    return exponent

@ti.func
def mip_from_pos(xyz, cascades):
    mx = ti.abs(xyz).max()
    # _, exponent = _frexp(mx)
    exponent = frexp_bit(ti.f32(mx))
    # frac, exponent = ti.frexp(ti.f32(mx))
    return ti.min(cascades-1, ti.max(0, exponent+1))

@ti.func
def mip_from_dt(dt, grid_size, cascades):
    # _, exponent = _frexp(dt*grid_size)
    exponent = frexp_bit(ti.f32(dt*grid_size))
    # frac, exponent = ti.frexp(ti.f32(dt*grid_size))
    return ti.min(cascades-1, ti.max(0, exponent))


#<----------------- hash table util code ----------------->

@ti.func
def random_in_unit_disk():
    theta = 2.0 * np.pi * ti.random()
    return ti.Vector([ti.sin(theta), ti.cos(theta)])

@ti.func
def random_normal():
    x = ti.random() * 2. - 1.
    y = ti.random() * 2. - 1.
    return tf_vec2(x, y)

@ti.func
def dir_encode_func(dir_):
    input = tf_vec32(0.0)
    dir = dir_/dir_.norm()
    x = dir[0]; y = dir[1]; z = dir[2]
    xy= x*y; xz= x*z; yz= y*z; x2= x*x; y2= y*y; z2= z*z
    
    temp = 0.28209479177387814
    input[0] = data_type(temp)
    input[1] = data_type(-0.48860251190291987*y)
    input[2] = data_type(0.48860251190291987*z)
    input[3] = data_type(-0.48860251190291987*x)
    input[4] = data_type(1.0925484305920792*xy)
    input[5] = data_type(-1.0925484305920792*yz)
    input[6] = data_type(0.94617469575755997*z2 - 0.31539156525251999)
    input[7] = data_type(-1.0925484305920792*xz)
    input[8] = data_type(0.54627421529603959*x2 - 0.54627421529603959*y2)
    input[9] = data_type(0.59004358992664352*y*(-3.0*x2 + y2))
    input[10] = data_type(2.8906114426405538*xy*z)
    input[11] = data_type(0.45704579946446572*y*(1.0 - 5.0*z2))
    input[12] = data_type(0.3731763325901154*z*(5.0*z2 - 3.0))
    input[13] = data_type(0.45704579946446572*x*(1.0 - 5.0*z2))
    input[14] = data_type(1.4453057213202769*z*(x2 - y2))
    input[15] = data_type(0.59004358992664352*x*(-x2 + 3.0*y2))

    return input

@ti.data_oriented
class NGP_fw:
    def __init__(self, grid_size, base_res, log2_T, res, level, exp_step_factor):
        self.res = res
        self.N_rays = res[0] * res[1]
        self.grid_size = grid_size
        self.grid_size3 = grid_size * grid_size * grid_size
        # self.exp_step_factor = exp_step_factor
        # self.scale = scale

        # rays intersection parameters
        # t1, t2 need to be initialized to -1.0
        self.hits_t = ti.Vector.field(n=2, dtype=data_type, shape=(self.N_rays))
        self.hits_t.fill(-1.0)
        # self.center = tf_vec3(0.0, 0.0, 0.0)
        self.center = ti.Vector.field(n=3, dtype=ti.f32, shape=())
        # self.xyz_min = -tf_vec3(scale, scale, scale)
        # self.xyz_max = tf_vec3(scale, scale, scale)
        # self.half_size = (self.xyz_max - self.xyz_min) / 2

        self.noise_buffer = ti.Vector.field(2, dtype=data_type, shape=(self.N_rays))
        self.gen_noise_buffer()

        self.rays_o = ti.Vector.field(n=3, dtype=data_type, shape=(self.N_rays))
        self.rays_d = ti.Vector.field(n=3, dtype=data_type, shape=(self.N_rays))


        # count the number of rays that still alive
        self.counter = ti.field(ti.i32, shape=())
        self.counter[None] = self.N_rays
        # current alive buffer index
        self.current_index = ti.field(ti.i32, shape=())
        self.current_index[None] = 0

        # how many samples that need to run the model
        self.model_launch = ti.field(ti.i32, shape=())

        # buffer for the alive rays
        # why times 2?
        self.alive_indices = ti.field(ti.i32, shape=(2*self.N_rays,))

        # padd the thread to the factor of block size (thread per block)
        self.padd_block_network = ti.field(ti.i32, shape=())
        self.padd_block_composite = ti.field(ti.i32, shape=())

        # hash table variables
        # self.min_samples = 1 if exp_step_factor==0 else 4
        # self.per_level_scales = 1.3195079565048218 # hard coded, otherwise it will be have lower percision
        self.base_res = base_res
        self.max_params = 2**log2_T
        self.level = level
        # hash table fields
        # self.offsets = ti.field(ti.i32, shape=(16,))
        # self.hash_map_sizes = ti.field(ti.uint32, shape=(16,))
        # self.hash_map_indicator = ti.field(ti.i32, shape=(16,))
        self.offsets = ti.types.vector(16, dtype=ti.i32)(0)
        self.hash_map_sizes = ti.types.vector(16, dtype=ti.uint32)(0)
        self.hash_map_indicator = ti.types.vector(16, dtype=ti.i32)(0)


        # buffers that used for points sampling 
        self.max_samples_per_rays = 1 if exp_step_factor==0. else 4
        self.max_samples_shape = self.N_rays * self.max_samples_per_rays

        self.xyzs = ti.Vector.field(3, dtype=data_type, shape=(self.max_samples_shape,))
        self.dirs = ti.Vector.field(3, dtype=data_type, shape=(self.max_samples_shape,))
        self.deltas = ti.field(data_type, shape=(self.max_samples_shape,))
        self.ts = ti.field(data_type, shape=(self.max_samples_shape,))

        # buffers that store the info of sampled points
        self.run_model_ind = ti.field(ti.int32, shape=(self.max_samples_shape,))
        self.N_eff_samples = ti.field(ti.int32, shape=(self.N_rays,))

        # intermediate buffers for network
        self.out_3 = ti.field(data_type, shape=(self.max_samples_shape, 3))
        self.out_1 = ti.field(data_type, shape=(self.max_samples_shape,))
        self.temp_hit = ti.field(ti.i32, shape=(self.max_samples_shape,))

        # results buffers
        self.opacity = ti.field(ti.f32, shape=(self.N_rays,))
        self.depth = ti.field(ti.f32, shape=(self.N_rays))
        self.rgb = ti.Vector.field(3, dtype=ti.f32, shape=(self.N_rays,))

        # GUI render buffer (data type must be float32)
        self.render_buffer = ti.Vector.field(3, dtype=ti.f32, shape=(res[1], res[0],))
        # camera parameters
        self.lookat = np.array([0.0, 0.0, -1.0])
        self.lookat_change = np.zeros((3,))
        self.lookup = np.array([0.0, -1.0, 0.0])

    def hash_table_init(self):
        print('----GridEncoding----')
        print(f'base_resolution: {self.base_res}') 
        print(f'log_scale:{self.per_level_scales:.5f}') 
        print(f'feature_per_level: {self.numof_hash_feet} logT: {self.max_params}')
        print(f'level: {self.level}')

        offset = 0
        for i in range(self.level):
            resolution = int(np.ceil(self.base_res * np.exp(i*np.log(self.per_level_scales)) - 1.0)) + 1
            params_in_level = resolution ** 3
            params_in_level = int(resolution ** 3) if params_in_level % 8 == 0 else int((params_in_level + 8 - 1) / 8) * 8
            params_in_level = min(self.max_params, params_in_level)
            self.offsets[i] = offset
            self.hash_map_sizes[i] = params_in_level
            self.hash_map_indicator[i] = 1 if resolution ** 3 <= params_in_level else 0
            offset += params_in_level
        offset *= self.numof_hash_feet
        error_mesg = f"hash shape don't match offset: {offset}, hash shape: {self.hash_embedding.shape[0]}, final res: {resolution}"
        assert offset == self.hash_embedding.shape[0], error_mesg
        

    def get_direction(self):
        w, h = int(self.res[1]), int(self.res[0])
        fx = self.K[None][0, 0]
        fy = self.K[None][1, 1]

        cx, cy = 0.5*w, 0.5*h

        x, y = np.meshgrid(
            np.arange(w, dtype=np.float32)+ 0.5,
            np.arange(h, dtype=np.float32)+ 0.5,
            indexing='xy'
        )

        directions = np.stack([(x-cx)/fx, (y-cy)/fy, np.ones_like(x)], -1)

        return directions.reshape(-1, 3)

    @ti.kernel
    # 根据图像w,h,相机内参(fx,fy,cx,cy)计算出每个pixel的方向(dx,dy,1)
    def init_direction(self, w: ti.i32, h: ti.i32):
        for i, j in ti.ndrange(w, h):
            ori_w = 2*self.K[None][0, 2]
            # ori_h = self.K[None][1, 2] * 2,
            scale = w / ori_w  
            # scale_h = h / ori_h
            fx = self.cam_fov_scale[None]*self.K[None][0, 0]*scale
            fy = self.cam_fov_scale[None]*self.K[None][1, 1]*scale
            cx, cy = 0.5*w, 0.5*h

            uni_dir = ti.Matrix([[(i+0.5-cx)/fx, (j+0.5-cy)/fy, 1.]], dt=data_type)
            self.directions[j*w+i] = uni_dir

    def load_model(self, model_path):
        print('Loading model from {}'.format(model_path))
        
        model = np.load(model_path, allow_pickle=True).item()

        np_hash = model['model.hash_encoder.params'].astype(np_type)
        np_sigma = model['model.xyz_encoder.params'].astype(np_type)
        np_rgb = model['model.rgb_net.params'].astype(np_type)
        np_bitfield = model['model.density_bitfield']
        self.rgb_depth = model['model.rgb_depth']
        self.cascades = model['model.cascade']
        self.numof_hash_feet = 2
        self.scale = float(model['model.box_scale'])
        self.xyz_min = -tf_vec3(self.scale)
        self.xyz_max = tf_vec3(self.scale)
        self.xyz_delta = self.xyz_max - self.xyz_min
        # self.half_size = self.xyz_delta / 2
        self.half_size = ti.Vector.field(3, dtype=ti.f32, shape=())
        self.half_size[None] = self.xyz_delta / 2
        self.exp_step_factor = 1/256 if self.scale > 0.5 else 0.
        self.min_samples = 1 if self.exp_step_factor==0. else 4

        self.per_level_scales = model['model.per_level_scale']
        self.net_width = model['model.n_neurons']
        self.sigma_n_input = model['model.sigma_n_input']
        # self.sigma_n_input = 16
        self.sigma_n_output = model['model.sigma_n_output']
        self.rgb_n_input = model['model.rgb_n_input']
        self.rgb_n_output =  model['model.rgb_n_output']

        # sigma_sm_per_weight = int(self.layer1_base / 128)
        # rgb_sm_per_weight = int(self.layer2_base / 128)
        self.sigma_model_size = np_sigma.shape[0]
        self.rgb_model_size = np_rgb.shape[0]
        self.sigma_sm_preload = int(self.sigma_model_size/block_dim)
        self.rgb_sm_preload = int(self.rgb_model_size/block_dim)

        self.sigma_layer1_base = self.net_width*self.sigma_n_input
        self.rgb_layer1_base = self.net_width*self.rgb_n_input
        self.rgb_layer2_base = self.rgb_layer1_base+self.net_width*self.net_width

        self.hash_embedding= ti.field(dtype=data_type, shape=(np_hash.shape[0],))
        self.sigma_weights= ti.field(dtype=data_type, shape=(self.sigma_model_size,))
        self.rgb_weights= ti.field(dtype=data_type, shape=(self.rgb_model_size,))
        # density_bitfield is used for point sampling
        self.density_bitfield = ti.field(ti.uint8, shape=(self.cascades*(self.grid_size**3)//8))

        self.hash_embedding.from_numpy(np_hash)
        self.sigma_weights.from_numpy(np_sigma)
        self.rgb_weights.from_numpy(np_rgb)
        self.density_bitfield.from_numpy(np_bitfield)

        self.xyzs_embedding = ti.field(data_type, shape=(self.max_samples_shape, self.sigma_n_input))
        self.final_embedding = ti.field(data_type, shape=(self.max_samples_shape, self.sigma_n_output))


        # use the pre-compute direction and scene pose
        self.directions = ti.Matrix.field(n=1, m=3, dtype=data_type, shape=(self.N_rays,))
        self.pose = ti.Matrix.field(n=4, m=4, dtype=data_type, shape=())
        self.K = ti.Matrix.field(n=3, m=3, dtype=data_type, shape=())
        self.cam_fov_scale = ti.field(dtype=data_type, shape=())
        self.cam_fov_scale[None] = 1.0

        self.K.from_numpy(model['K'].astype(np_type))
        self.default_rot = model['poses'][20].astype(np_type)[:3, :3]
        self.cam = OrbitCamera(self.default_rot, r=2.5)
        # self.pose.from_numpy(model['poses'][20].astype(np_type))
        # if self.res[0] != 800 or self.res[1] != 800:
        #     directions = self.get_direction(model['camera_angle_x'])[:, None, :].astype(np_type)
        # else:
        # directions = model['directions'][:, None, :].astype(np_type)
        # self.directions.from_numpy(directions)
        self.init_direction(self.res[1], self.res[0])
        print('----model loaded----')
        print('K: ')
        print(self.K)
        print('pose: ')
        print(self.pose)
        print("half size: ", self.half_size[None])
        print('rgb_depth: ', self.rgb_depth)
        print('cascades: ', self.cascades)
        print('scale: ', self.scale)
        print('per_level_scales: ', self.per_level_scales)
        print('net_width: ', self.net_width)
        print('sigma_n_input: ', self.sigma_n_input)
        print('sigma_n_output: ', self.sigma_n_output)
        print('rgb_n_input: ', self.rgb_n_input)
        print('rgb_n_output: ', self.rgb_n_output)
        print('sigma_model_size: ', self.sigma_model_size)
        print('rgb_model_size: ', self.rgb_model_size)
        print('sigma_sm_preload: ', self.sigma_sm_preload)
        print('rgb_sm_preload: ', self.rgb_sm_preload)


    @staticmethod
    def taichi_init(kernel_profiler):
        ti.init(
            arch=arch, 
            offline_cache=True,
            kernel_profiler=kernel_profiler, 
            enable_fallback=False, 
        )

    @staticmethod
    def taichi_print_profiler():
        ti.profiler.print_kernel_profiler_info()

    @ti.kernel
    def reset(self):
        self.depth.fill(0.0)
        self.opacity.fill(0.0)
        self.counter[None] = self.N_rays
        for i, j in ti.ndrange(self.N_rays, 2):
            self.alive_indices[i*2+j] = i    

    @ti.func
    def _ray_aabb_intersec(self, ray_o, ray_d):
        inv_d = 1.0 / ray_d

        # half_size = tf_vec3(self.half_size[None])

        t_min = (self.center[None]-self.half_size[None]-ray_o)*inv_d
        t_max = (self.center[None]+self.half_size[None]-ray_o)*inv_d

        _t1 = ti.min(t_min, t_max)
        _t2 = ti.max(t_min, t_max)
        t1 = _t1.max()
        t2 = _t2.min()

        return tf_vec2(t1, t2)


    @ti.kernel
    # for each ray generate random noise offset (nx,ny in [-1,1])
    def gen_noise_buffer(self):
        for i in range(self.N_rays):
            self.noise_buffer[i] = random_normal()
            # self.noise_buffer[i] = random_in_unit_disk()

    # dof: consider len's distortion
    # dist_to_focus=1.2 len_dis=0.04
    @ti.kernel
    def ray_intersect_dof(self, dist_to_focus: float, len_dis: float):
        ti.block_local(self.pose)
        for i in self.directions: 
            c2w = self.pose[None]
            dir_ori = self.directions[i]
            offset = len_dis*self.noise_buffer[i]
            offset_m = tf_mat1x3(
                [[
                    offset[0],
                    offset[1],
                    0.0,
                ]]
            )
            c2w_dir = c2w[:3, :3].transpose()
            offset_w = offset_m @ c2w_dir
            mat_result = (dir_ori*dist_to_focus) @ c2w_dir - offset_w
            ray_d = tf_vec3(mat_result[0, 0], mat_result[0, 1],mat_result[0, 2])
            ray_o = c2w[:3, 3] + tf_vec3(offset_w[0, 0], offset_w[0, 1],offset_w[0, 2])
            
            t1t2 = self._ray_aabb_intersec(ray_o, ray_d)

            if t1t2[1] > 0.0:
                self.hits_t[i][0] = data_type(ti.max(t1t2[0], NEAR_DISTANCE))
                self.hits_t[i][1] = t1t2[1]  

            self.rays_o[i] = ray_o
            self.rays_d[i] = ray_d

    # for each ray get intersect with aabb box
    # self.hits_t: two intersect point t1&t2
    # self.rays_o,self.rays_d (ray in world coord)
    @ti.kernel
    def ray_intersect(self):
        ti.block_local(self.pose)
        for i in self.directions: 
            c2w = self.pose[None]
            mat_result = self.directions[i] @ c2w[:3, :3].transpose()
            ray_d = tf_vec3(mat_result[0, 0], mat_result[0, 1],mat_result[0, 2])
            ray_o = c2w[:3, 3]
            
            t1t2 = self._ray_aabb_intersec(ray_o, ray_d)

            if t1t2[1] > 0.0:
                self.hits_t[i][0] = data_type(ti.max(t1t2[0], NEAR_DISTANCE))
                self.hits_t[i][1] = t1t2[1]  

            self.rays_o[i] = ray_o
            self.rays_d[i] = ray_d

    @ti.kernel
    def raymarching_test_kernel(self, N_samples: int):

        self.run_model_ind.fill(0)
        for n in ti.ndrange(self.counter[None]):
            c_index = self.current_index[None]
            r = self.alive_indices[n*2+c_index]
            grid_size_inv = 1.0/self.grid_size

            ray_o = self.rays_o[r]
            ray_d = self.rays_d[r]
            t1t2 = self.hits_t[r]

            d_inv = 1.0/ray_d

            t = t1t2[0]
            t2 = t1t2[1]

            s = 0

            start_idx = n * N_samples

            while (t<t2) & (s<N_samples):
                # xyz = ray_o + t*ray_d
                xyz = ray_o + t*ray_d
                # dt is t regular in grid
                dt = calc_dt(t, self.exp_step_factor, self.grid_size, self.scale)
                mip = ti.max(mip_from_pos(xyz, self.cascades),
                             mip_from_dt(dt, self.grid_size, self.cascades))

                mip_bound = ti.min(ti.pow(2., mip-1), self.scale) # current gird bound
                # mip = 0
                # mip_bound = 0.5
                mip_bound_inv = 1/mip_bound

                # nxyz is xyz regular in grid
                nxyz = ti.math.clamp(0.5*(xyz*mip_bound_inv+1)*self.grid_size, 0.0, self.grid_size-1.0)
                # nxyz = ti.ceil(nxyz)

                idx = mip*self.grid_size3 + __morton3D(ti.cast(nxyz, ti.u32))
                # occ = density_grid_taichi[idx] > 5.912066756501768
                occ = self.density_bitfield[ti.u32(idx//8)] & (1 << ti.u32(idx%8))

                if occ: # if occ, get one smaple in ray
                    sn = start_idx + s
                    for p in ti.static(range(3)):
                        self.xyzs[sn][p] = xyz[p]
                        self.dirs[sn][p] = ray_d[p]
                    self.run_model_ind[sn] = 1
                    self.ts[sn] = t
                    self.deltas[sn] = dt
                    t += dt
                    self.hits_t[r][0] = t
                    s += 1

                else: # if not occ, ray marching
                    txyz = (((nxyz+0.5+0.5*ti.math.sign(ray_d))*grid_size_inv*2-1)*mip_bound-xyz)*d_inv

                    t_target = t + ti.max(0, txyz.min())
                    t += calc_dt(t, self.exp_step_factor, self.grid_size, self.scale)
                    while t < t_target:
                        t += calc_dt(t, self.exp_step_factor, self.grid_size, self.scale)

            self.N_eff_samples[n] = s
            if s == 0:
                self.alive_indices[n*2+c_index] = -1

    @ti.kernel
    def rearange_index(self, B: ti.i32):
        self.model_launch[None] = 0
        
        for i in ti.ndrange(B):
            if self.run_model_ind[i]:
                index = ti.atomic_add(self.model_launch[None], 1)
                self.temp_hit[index] = i

        self.model_launch[None] += 1
        self.padd_block_network[None] = ((self.model_launch[None]+ block_dim - 1)// block_dim) * block_dim
        # self.padd_block_composite[None] = ((self.counter[None]+ 128 - 1)// 128) *128

    @ti.kernel
    def hash_encode(self):
        # get hash table embedding
        # ti.loop_config(block_dim=16)
        # for level in ti.static(range(16)):
        for sn in ti.ndrange(self.model_launch[None]):
            for level in ti.static(range(self.level)):
                # normalize to [0, 1], before is [-0.5, 0.5]
                # xyz = self.xyzs[self.temp_hit[sn]] + 0.5
                xyz = (self.xyzs[self.temp_hit[sn]] - self.xyz_min) / (self.xyz_delta)
                offset = self.offsets[level] * self.numof_hash_feet
                indicator = self.hash_map_indicator[level]
                map_size = self.hash_map_sizes[level]

                init_val0 = tf_vec1(0.0)
                init_val1 = tf_vec1(1.0)
                local_feature_0 = init_val0[0]
                local_feature_1 = init_val0[0]

                index_temp = tf_index_temp(0)
                w_temp = tf_vec8(0.0)
                hash_temp_1 = tf_vec8(0.0)
                hash_temp_2 = tf_vec8(0.0)

                scale = self.base_res * ti.exp(level*ti.log(self.per_level_scales)) - 1.0
                resolution = ti.cast(ti.ceil(scale), ti.uint32) + 1

                pos = xyz * scale + 0.5
                pos_grid_uint = ti.cast(ti.floor(pos), ti.uint32)
                pos -= pos_grid_uint
                # pos_grid_uint = ti.cast(pos_grid, ti.uint32)

                for idx in ti.static(range(8)):
                    # idx_uint = ti.cast(idx, ti.uint32)
                    w = init_val1[0]
                    pos_grid_local = uvec3(0)

                    for d in ti.static(range(3)):
                        if (idx & (1 << d)) == 0:
                            pos_grid_local[d] = pos_grid_uint[d]
                            w *= data_type(1 - pos[d])
                        else:
                            pos_grid_local[d] = pos_grid_uint[d] + 1
                            w *= data_type(pos[d])

                    index = ti.int32(grid_pos2hash_index(indicator, pos_grid_local, resolution, map_size))
                    index_temp[idx] = offset+index*self.numof_hash_feet
                    w_temp[idx] = w

                    # local_feature_0 += data_type(w * self.hash_embedding[offset+index*2])
                    # local_feature_1 += data_type(w * self.hash_embedding[offset+index*2+1])

                for idx in ti.static(range(8)):
                    hash_temp_1[idx] = self.hash_embedding[index_temp[idx]]
                    hash_temp_2[idx] = self.hash_embedding[index_temp[idx]+1]

                for idx in ti.static(range(8)):
                    local_feature_0 += data_type(w_temp[idx] * hash_temp_1[idx])
                    local_feature_1 += data_type(w_temp[idx] * hash_temp_2[idx])

                self.xyzs_embedding[sn, level*self.numof_hash_feet] = local_feature_0
                self.xyzs_embedding[sn, level*self.numof_hash_feet+1] = local_feature_1


    @ti.kernel
    def sigma_layer(self):
        ti.loop_config(block_dim=block_dim)
        for sn in ti.ndrange(self.padd_block_network[None]):
            tid = sn % block_dim
            did_launch_num = self.model_launch[None]
            init_val = tf_vec1(0.0)
            input_val = ti.simt.block.SharedArray((self.sigma_n_input, block_dim), data_type)
            weight = ti.simt.block.SharedArray((self.sigma_model_size,), data_type)
            hid1 = ti.simt.block.SharedArray((self.net_width, block_dim), data_type)
            hid2 = ti.simt.block.SharedArray((self.net_width, block_dim), data_type)
            for i in ti.static(range(self.sigma_sm_preload)):
                k = tid*self.sigma_sm_preload+i
                weight[k] = self.sigma_weights[k] # mlp weight?
            for i in ti.static(range(self.sigma_n_input)):
                input_val[i, tid] = self.xyzs_embedding[sn, i] # hash encode output
            ti.simt.block.sync()

            if sn < did_launch_num:
                # input_val = tf_vec32(0.0)
            
                for i in range(self.net_width):
                    temp = init_val[0]
                    for j in ti.static(range(self.sigma_n_input)):
                        temp += input_val[j, tid] * weight[i*self.sigma_n_input+j]

                    hid1[i, tid] = temp
                # ti.simt.block.sync()
                
                for i in range(self.sigma_n_output):
                    temp = init_val[0]
                    for j in ti.static(range(self.net_width)):
                        temp += data_type(ti.max(0.0, hid1[j, tid])) * weight[self.sigma_layer1_base+i*self.net_width+j]
                    hid2[i, tid] = temp
                # ti.simt.block.sync()

                self.out_1[self.temp_hit[sn]] = data_type(ti.exp(hid2[0, tid])) # sigma output
                for i in ti.static(range(self.sigma_n_output)):
                    self.final_embedding[sn, i] = hid2[i, tid] # input to rgb mlp
                
                # ti.simt.block.sync()

    @ti.kernel
    def rgb_layer(self):
        ti.loop_config(block_dim=block_dim)
        for sn in ti.ndrange(self.padd_block_network[None]):
            ray_id = self.temp_hit[sn]
            tid = sn % block_dim
            did_launch_num = self.model_launch[None]
            init_val = tf_vec1(0.0)
            weight = ti.simt.block.SharedArray((self.rgb_model_size,), data_type)
            hid1 = ti.simt.block.SharedArray((self.net_width, block_dim), data_type)
            hid2 = ti.simt.block.SharedArray((self.net_width, block_dim), data_type)
            for i in ti.static(range(self.rgb_sm_preload)):
                k = tid*self.rgb_sm_preload+i
                weight[k] = self.rgb_weights[k]
            ti.simt.block.sync()

            if sn < did_launch_num:
                
                dir_ = self.dirs[ray_id]
                input = dir_encode_func(dir_) # direction encode,what about self.dir_encode()

                for i in ti.static(range(16)):
                    input[16+i] = self.final_embedding[sn, i] # contain position encode,sigma mlp output

                for i in range(self.net_width):
                    temp = init_val[0]
                    for j in ti.static(range(self.rgb_n_input)):
                        temp += input[j] * weight[i*self.rgb_n_input+j] # mlp forward

                    hid1[i, tid] = temp

                if ti.static(self.rgb_depth == 2): # two mlp?
                    for i in range(self.net_width):
                        temp = init_val[0]
                        for j in ti.static(range(self.net_width)):
                            temp += data_type(ti.max(0.0, hid1[j, tid])) * weight[self.rgb_layer1_base+i*self.net_width+j]

                        hid2[i, tid] = temp

                    for i in ti.static(range(self.rgb_n_output)):
                        temp = init_val[0]
                        for j in ti.static(range(self.net_width)):
                            temp += data_type(ti.max(0.0, hid2[j, tid])) * weight[self.rgb_layer2_base+i*self.net_width+j]

                        hid1[i, tid] = temp


                    for i in range(self.rgb_n_output):
                        self.out_3[self.temp_hit[sn], i] = data_type(1 / (1 + ti.exp(-hid1[i, tid]))) # rgb output
                else:
                    for i in range(self.rgb_n_output):
                        temp = init_val[0]
                        for j in ti.static(range(self.net_width)):
                            temp += data_type(ti.max(0.0, hid1[j, tid])) * weight[self.net_width*self.rgb_n_input+i*self.net_width+j]

                        hid2[i, tid] = temp
                    # ti.simt.block.sync()

                    for i in ti.static(range(self.rgb_n_output)):
                        self.out_3[self.temp_hit[sn], i] = data_type(1 / (1 + ti.exp(-hid2[i, tid])))
                        
                # ti.simt.block.sync()


    @ti.kernel
    def composite_test(self, max_samples: ti.i32, T_threshold: data_type):
        for n in ti.ndrange(self.counter[None]): # for each ray
            N_samples = self.N_eff_samples[n]
            if N_samples != 0:
                c_index = self.current_index[None]
                r = self.alive_indices[n*2+c_index]

                T = data_type(1.0 - self.opacity[r])

                start_idx = n * max_samples

                rgb_temp = tf_vec3(0.0)
                depth_temp = tf_vec1(0.0)
                opacity_temp = tf_vec1(0.0)
                out_3_temp = tf_vec3(0.0)

                for s in range(N_samples): # for each sample along ray
                    sn = start_idx + s
                    a = data_type(1.0 - ti.exp(-self.out_1[sn]*self.deltas[sn]))
                    w = a * T # opacity=1,T=0,w=0,in the air without rgb

                    for i in ti.static(range(3)):
                        out_3_temp[i] = self.out_3[sn, i]

                    rgb_temp += w * out_3_temp # sigma*rgb_out
                    depth_temp[0] += w * self.ts[sn] # sigma*length
                    opacity_temp[0] += w # occ=sum(sigma)

                    T *= data_type(1.0 - a)

                    if T <= T_threshold: # if <0.01, de-alive, stop integral along ray
                        self.alive_indices[n*2+c_index] = -1
                        break


                self.rgb[r] += rgb_temp
                self.depth[r] += depth_temp[0]
                self.opacity[r] += opacity_temp[0]

    @ti.kernel
    def re_order(self, B: ti.i32):

        self.counter[None] = 0
        c_index = self.current_index[None]
        n_index = (c_index + 1) % 2
        self.current_index[None] = n_index

        for i in ti.ndrange(B):
            alive_temp = self.alive_indices[i*2+c_index]
            if alive_temp >= 0:
                index = ti.atomic_add(self.counter[None], 1)
                self.alive_indices[index*2+n_index] = alive_temp


    def write_image(self):
        rgb_np = self.rgb.to_numpy().reshape(self.res[0], self.res[1], 3)
        depth_np = self.depth.to_numpy().reshape(self.res[0], self.res[1])
        plt.imsave('taichi_ngp.png', (rgb_np*255).astype(np.uint8))
        plt.imsave('taichi_ngp_depth.png', depth2img(depth_np))

    def render(self, max_samples, T_threshold, use_dof=False, dist_to_focus=0.8, len_dis=0.0, clear_rgb=True) -> Tuple[float, int, int]:
        samples = 0
        if clear_rgb:
            self.rgb.fill(0.0)
        self.reset()
        self.gen_noise_buffer()
        if use_dof:
            self.ray_intersect_dof(dist_to_focus, len_dis)
        else:
            self.ray_intersect()

        while samples < max_samples: # max_sample in one frame
            N_alive = self.counter[None]
            if N_alive == 0: break

            # how many more samples the number of samples add for each ray
            N_samples = max(min(self.N_rays//N_alive, 64), self.min_samples)
            samples += N_samples
            launch_model_total = N_alive * N_samples
            # print(f"samples: {samples}, N_alive: {N_alive}, N_samples: {N_samples}")
            
            # raymarching using cascades occ grids, to sampling sufface effective
            self.raymarching_test_kernel(N_samples)
            
            self.rearange_index(launch_model_total)
            # dir encode for direction
            # self.dir_encode()
            # hash encode for position(xyz)
            self.hash_encode()
            # sigma mlp
            self.sigma_layer()
            # rgb mlp
            self.rgb_layer()
            # self.FullyFusedMLP()
            self.composite_test(N_samples, T_threshold)
            self.re_order(N_alive)

        return samples, N_alive, N_samples

    def render_frame(self, n=1):
        t = time.time()
        for _ in range(n):
            samples, N_alive, N_samples = self.render(max_samples=100, T_threshold=1e-2, clear_rgb=True)
        ti.sync()
        print(f"samples: {samples}, N_alive: {N_alive}, N_samples: {N_samples}")
        print(f'Render time: {1000*(time.time()-t)/n:.2f} ms')   

        self.write_image()

    @ti.kernel
    def rgb_to_render_buffer(self, frame: ti.i32):
        for i, j in self.render_buffer:
            rgb = self.rgb[(self.res[0]-j)*self.res[1]+i]
            self.render_buffer[i, j] = rgb / frame

    @ti.kernel
    def depth_max(self) -> vec2:
        max_v = self.depth[0]
        min_v = self.depth[0]
        for i in ti.ndrange(self.N_rays):
            ti.atomic_max(max_v, self.depth[i])
            ti.atomic_min(min_v, self.depth[i])
        return vec2(max_v, min_v)

    @ti.kernel
    def depth_to_render_buffer(self, max_min: vec2):
        for i, j in self.render_buffer:
            max_v = max_min[0]
            min_v = max_min[1]
            depth = self.depth[(self.res[0]-j)*self.res[1]+i]
            pixel = (vec3(depth)-min_v)/(max_v-min_v)
            self.render_buffer[i, j] = pixel

    def init_cam(self):
        self.lookat = self.lookat @ self.pose.to_numpy()[:, :3].T

    def render_gui(self):

        video_manager = None

        # check if the export file exists for snapshot and video
        export_dir = './export/'
        if not os.path.exists(export_dir):
            os.mkdir(export_dir)

        H, W = self.res
        window = ti.ui.Window('Taichi NGP', (W, H))
        canvas = window.get_canvas()
        gui = window.get_gui()

        last_orbit_x = None
        last_orbit_y = None
        rotate_speed = 50
        movement_speed = 0.03
        max_samples_for_rendering = 100
        render_time = 0
        # white_bg = False
        recording = False
        show_depth = False
        use_dof = False
        last_use_dof = False
        frame = 0
        T_threshold = 1e-2
        dist_to_focus = 1.2
        len_dis=0.04
        self.init_cam()
        last_pose = self.pose.to_numpy()
        total_frame = 0
        last_dist_to_focus = dist_to_focus
        last_len_dis = len_dis

        box_size_x = self.half_size[None][0]
        box_size_y = self.half_size[None][1]
        box_size_z = self.half_size[None][2]
        last_box_size_x = box_size_x
        last_box_size_y = box_size_y
        last_box_size_z = box_size_z

        center_x = self.center[None][0]
        center_y = self.center[None][1]
        center_z = self.center[None][2]
        last_center_x = center_x
        last_center_y = center_y
        last_center_z = center_z

        cam_fov_scale = 1.0
        last_cam_fov_scale = cam_fov_scale

        while window.running:
            # TODO: make it more efficient
            total_frame+=1

            if window.is_pressed(ti.ui.RMB):
                curr_mouse_x, curr_mouse_y = window.get_cursor_pos()
                if last_orbit_x is None or last_orbit_y is None:
                    last_orbit_x, last_orbit_y = curr_mouse_x, curr_mouse_y
                else:
                    dx = curr_mouse_x - last_orbit_x
                    dy = curr_mouse_y - last_orbit_y
                    self.cam.orbit(dx, -dy)
                    last_orbit_x, last_orbit_y = curr_mouse_x, curr_mouse_y
            else:
                last_orbit_x = None
                last_orbit_y = None

            if window.is_pressed('w'):
                self.cam.scale(0.2)
            if window.is_pressed('s'):
                self.cam.scale(-0.2)
            if window.is_pressed('a'):
                self.cam.pan(100, 0.)
            if window.is_pressed('d'):
                self.cam.pan(-100, 0.)
            if window.is_pressed('e'):
                self.cam.pan(0., -100)
            if window.is_pressed('q'):
                self.cam.pan(0., 100)

            if self.cam.params_changed:
                self.rgb.fill(0.0)
                total_frame = 1
                self.cam.params_changed = False
                self.pose.from_numpy(self.cam.pose.astype(np_type))

            with gui.sub_window("Options", 0.05, 0.05, 0.68, 0.3) as w:
                w.text(f'General')
                box_size_x = w.slider_float('box size_x', box_size_x, 0.01, 64.0)
                box_size_y = w.slider_float('box size_y', box_size_y, 0.01, 64.0)
                box_size_z = w.slider_float('box size_z', box_size_z, 0.01, 64.0)
                center_x = w.slider_float('center_x', center_x, -64.0, 64.0)
                center_y = w.slider_float('center_y', center_y, -64.0, 64.0)
                center_z = w.slider_float('center_z', center_z, -64.0, 64.0)
                cam_fov_scale = w.slider_float('camera fov', cam_fov_scale, 0.01, 2.0)
                T_threshold = w.slider_float('transparency threshold', T_threshold, 0., 1.)
                max_samples_for_rendering = w.slider_float("max samples", max_samples_for_rendering, 1, 100)
                show_depth = w.checkbox("show depth", show_depth)
                # white_bg = w.checkbox("white background", white_bg)
                if last_box_size_x != box_size_x or last_box_size_y != box_size_y or last_box_size_z != box_size_z:
                    last_box_size_x = box_size_x
                    last_box_size_y = box_size_y
                    last_box_size_z = box_size_z
                    self.half_size[None] = vec3(box_size_x, box_size_y, box_size_z)
                    self.rgb.fill(0.0)
                    total_frame = 1
                
                if last_center_x != center_x or last_center_y != center_y or last_center_z != center_z:
                    last_center_x = center_x
                    last_center_y = center_y
                    last_center_z = center_z
                    self.center[None] = vec3(center_x, center_y, center_z)
                    self.rgb.fill(0.0)
                    total_frame = 1

                if last_cam_fov_scale != cam_fov_scale:
                    last_cam_fov_scale = cam_fov_scale
                    self.cam_fov_scale[None] = cam_fov_scale
                    self.init_direction(self.res[1], self.res[0])
                    self.rgb.fill(0.0)
                    total_frame = 1

                w.text(f'Camera')
                use_dof = w.checkbox("apply depth of field", use_dof)
                dist_to_focus = w.slider_float("focus distance", dist_to_focus, 0.8, 3.)
                len_dis = w.slider_float('lens size', len_dis, 0., 0.1)
                if last_dist_to_focus != dist_to_focus or last_len_dis != len_dis or last_use_dof != use_dof:
                    last_dist_to_focus = dist_to_focus
                    last_len_dis = len_dis
                    last_use_dof = use_dof
                    self.rgb.fill(0.0)
                    total_frame = 1

                w.text(f'Render time: {render_time:.2f} ms')

            with gui.sub_window("Export", 0.75, 0.05, 0.2, 0.1) as w:
                if gui.button("snapshot "):
                    ti.tools.imwrite(self.render_buffer.to_numpy(), export_dir+'snap_shot.png')
                    print("save snapshot in export folder") 
                if gui.button('recording'):
                    frame = 0
                    if not recording:
                        video_manager = ti.tools.VideoManager(output_dir=export_dir, framerate=24, automatic_build=False)
                        recording = True
                    else:
                        recording = False
                        video_manager.make_video(gif=True, mp4=True)
                        print("save video in export folder") 

                if recording and video_manager:
                    w.text(f'recording frames: {frame}')
                    frame += 1
                    pixels_img = self.render_buffer.to_numpy()
                    video_manager.write_frame(pixels_img)

            t = time.time()
            _, _, _ = self.render(
                max_samples=max_samples_for_rendering, 
                T_threshold=T_threshold, 
                use_dof=use_dof,
                dist_to_focus=dist_to_focus,
                len_dis=len_dis,
                clear_rgb=False
            )

            if not show_depth:
                self.rgb_to_render_buffer(total_frame)
            else:
                self.depth_to_render_buffer(self.depth_max())

            render_time = 1000*(time.time()-t)
            canvas.set_image(self.render_buffer)
            window.show()

def main(args):
    NGP_fw.taichi_init(args.print_profile)
    real = args.real
    res = [args.h, args.w]
    if args.scene in ['bonsai', 'counter', 'garden', 'kitchen', 'bicycle'] and not args.real:
        real = True
        res = [840, 1296]
        
    ngp = NGP_fw(  
        grid_size=128, 
        base_res=16, 
        log2_T=19, 
        # res=[res, res],
        # 840, 1296 for real
        res=res,
        level=16, 
        exp_step_factor=0 if not real else 1/256,
    )
    if args.model_path:
        ngp.load_model(args.model_path)
    else:
        model_dir = './npy_models/'
        if not os.path.exists(model_dir):
            os.mkdir(model_dir)
        npy_file = os.path.join(model_dir, args.scene+'.npy')
        if not os.path.exists(npy_file):
            print(f"No {args.scene} model found, downloading ...")
            url = PRETRAINED_MODEL_URL.format(args.scene)
            wget.download(url, out=npy_file)
        ngp.load_model(npy_file)

    ngp.hash_table_init()

    if not args.gui:
        ngp.render_frame(args.run_n)
    else:
        ngp.render_gui()

    if args.print_profile:
        NGP_fw.taichi_print_profiler()

if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--w', type=int, default=800)
    parser.add_argument('--h', type=int, default=800)
    parser.add_argument('--run_n', type=int, default=1)
    parser.add_argument('--scene', type=str, default='lego',
                        choices=[
                            # synthetic scenes
                            'ship', 'mic', 'materials', 'lego', 'hotdog', 'ficus', 'drums', 'chair', 
                            # real scenes
                            'garden', 'bonsai', 'counter', 'garden', 'kitchen', 'bicycle'
                        ],)
    parser.add_argument('--model_path', type=str, default=None)
    parser.add_argument('--gui', action='store_true', default=False)
    parser.add_argument('--print_profile', action='store_true', default=False)
    parser.add_argument('--real', action='store_true', default=False)
    args = parser.parse_args()
    main(args)