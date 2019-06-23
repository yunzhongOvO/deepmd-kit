import os,warnings
import numpy as np
import tensorflow as tf
from deepmd.common import j_must_have, j_must_have_d, j_have
from deepmd.Model import Model

from deepmd.RunOptions import global_tf_float_precision
from deepmd.RunOptions import global_np_float_precision
from deepmd.RunOptions import global_ener_float_precision
from deepmd.RunOptions import global_cvt_2_tf_float
from deepmd.RunOptions import global_cvt_2_ener_float

module_path = os.path.dirname(os.path.realpath(__file__)) + "/"
assert (os.path.isfile (module_path  + "libop_abi.so" )), "op module does not exist"
op_module = tf.load_op_library(module_path + "libop_abi.so")

class ModelSeR (Model):
    def __init__ (self, jdata):
        # descrpt config
        self.use_smooth = False
        self.sel_r = j_must_have (jdata, 'sel_r')
        self.sel_a = [ 0 for ii in range(len(self.sel_r)) ]
        self.sel = self.sel_r
        if j_have (jdata, 'sel_a') :
            warnings.warn ('ignoring key sel_a in the json database and set sel_r to %s' % str(self.sel_a))
        self.ntypes = len(self.sel_r)
        self.rcut = j_must_have (jdata, 'rcut')
        if j_have(jdata, 'rcut_smth') :
            self.rcut_smth = jdata['rcut_smth']
        else :
            self.rcut_smth = self.rcut
        # fparam
        self.numb_fparam = 0
        if j_have(jdata, 'numb_fparam') :
            self.numb_fparam = jdata['numb_fparam']
	# type_map
        self.type_map = []
        if j_have(jdata, 'type_map') :
            self.numb_fparam = jdata['type_map']
        # filter of smooth version
        if j_have(jdata, 'coord_norm') :
            self.coord_norm = jdata['coord_norm']
        else :
            self.coord_norm = True
        self.filter_neuron = j_must_have (jdata, 'filter_neuron')
        self.filter_resnet_dt = False
        if j_have(jdata, 'filter_resnet_dt') :
            self.filter_resnet_dt = jdata['filter_resnet_dt']        
        # numb of neighbors and numb of descrptors
        self.nnei_a = np.cumsum(self.sel_a)[-1]
        self.nnei_r = np.cumsum(self.sel_r)[-1]
        self.nnei = np.cumsum(self.sel)[-1]
        self.ndescrpt_a = self.nnei_a * 4
        self.ndescrpt_r = self.nnei_r * 1
        self.ndescrpt = self.nnei_r
        # network size
        self.n_neuron = j_must_have_d (jdata, 'fitting_neuron', ['n_neuron'])
        self.resnet_dt = True
        if j_have(jdata, 'resnet_dt') :
            warnings.warn("the key \"%s\" is deprecated, please use \"%s\" instead" % ('resnet_dt','fitting_resnet_dt'))
            self.resnet_dt = jdata['resnet_dt']
        if j_have(jdata, 'fitting_resnet_dt') :
            self.resnet_dt = jdata['fitting_resnet_dt']
        self.type_fitting_net = False            

        # short-range tab
        if 'use_srtab' in jdata :
            self.srtab = TabInter(jdata['use_srtab'])
            self.smin_alpha = j_must_have(jdata, 'smin_alpha')
            self.sw_rmin = j_must_have(jdata, 'sw_rmin')
            self.sw_rmax = j_must_have(jdata, 'sw_rmax')
        else :
            self.srtab = None

        self.seed = None
        if j_have (jdata, 'seed') :
            self.seed = jdata['seed']
        self.useBN = False

    def get_rcut (self) :
        return self.rcut

    def get_ntypes (self) :
        return self.ntypes

    def get_numb_fparam (self) :
        return self.numb_fparam

    def get_type_map (self) :
        return self.type_map

    def compute_dstats (self,
                        data_coord, 
                        data_box, 
                        data_atype, 
                        natoms_vec,
                        mesh,
                        reuse = None) :    
        all_davg = []
        all_dstd = []
        sumr = []
        sumn = []
        sumr2 = []
        for cc,bb,tt,nn,mm in zip(data_coord,data_box,data_atype,natoms_vec,mesh) :
            sysr,sysr2,sysn \
                = self._compute_dstats_sys_se_r(cc,bb,tt,nn,mm,reuse)
            sumr.append(sysr)
            sumn.append(sysn)
            sumr2.append(sysr2)
        sumr = np.sum(sumr, axis = 0)
        sumn = np.sum(sumn, axis = 0)
        sumr2 = np.sum(sumr2, axis = 0)
        for type_i in range(self.ntypes) :
            davgunit = [sumr[type_i]/sumn[type_i]]
            dstdunit = [self._compute_std(sumr2[type_i], sumr[type_i], sumn[type_i])]
            davg = np.tile(davgunit, self.ndescrpt // 1)
            dstd = np.tile(dstdunit, self.ndescrpt // 1)
            all_davg.append(davg)
            all_dstd.append(dstd)

        davg = np.array(all_davg)
        dstd = np.array(all_dstd)

        return davg, dstd


    def build_interaction (self, 
                           coord_, 
                           atype_,
                           natoms,
                           box, 
                           mesh,
                           fparam,
                           davg = None, 
                           dstd = None,
                           bias_atom_e = None,
                           suffix = '', 
                           reuse_attr = None,
                           reuse_weights = None):
        with tf.variable_scope('model_attr' + suffix, reuse = reuse_attr) :
            if davg is None:
                davg = np.zeros([self.ntypes, self.ndescrpt]) 
            if dstd is None:
                dstd = np.ones ([self.ntypes, self.ndescrpt])
            t_rcut = tf.constant(self.rcut, 
                                 name = 'rcut', 
                                 dtype = global_tf_float_precision)
            t_ntypes = tf.constant(self.ntypes, 
                                   name = 'ntypes', 
                                   dtype = tf.int32)
            t_dfparam = tf.constant(self.numb_fparam, 
                                    name = 'dfparam', 
                                    dtype = tf.int32)
            t_tmap = tf.constant(' '.join(self.type_map), 
                                 name = 'tmap', 
                                 dtype = tf.string)
            self.t_avg = tf.get_variable('t_avg', 
                                         davg.shape, 
                                         dtype = global_tf_float_precision,
                                         trainable = False,
                                         initializer = tf.constant_initializer(davg, dtype = global_tf_float_precision))
            self.t_std = tf.get_variable('t_std', 
                                         dstd.shape, 
                                         dtype = global_tf_float_precision,
                                         trainable = False,
                                         initializer = tf.constant_initializer(dstd, dtype = global_tf_float_precision))
            if self.srtab is not None :
                tab_info, tab_data = self.srtab.get()
                self.tab_info = tf.get_variable('t_tab_info',
                                                tab_info.shape,
                                                dtype = tf.float64,
                                                trainable = False,
                                                initializer = tf.constant_initializer(tab_info, dtype = tf.float64))
                self.tab_data = tf.get_variable('t_tab_data',
                                                tab_data.shape,
                                                dtype = tf.float64,
                                                trainable = False,
                                                initializer = tf.constant_initializer(tab_data, dtype = tf.float64))

        coord = tf.reshape (coord_, [-1, natoms[1] * 3])
        atype = tf.reshape (atype_, [-1, natoms[1]])

        descrpt, descrpt_deriv, rij, nlist \
            = op_module.descrpt_se_r (coord,
                                      atype,
                                      natoms,
                                      box,                                    
                                      mesh,
                                      self.t_avg,
                                      self.t_std,
                                      rcut = self.rcut,
                                      rcut_smth = self.rcut_smth,
                                      sel = self.sel_r)

        descrpt_reshape = tf.reshape(descrpt, [-1, self.ndescrpt])

        atom_ener = self.build_atom_net (descrpt_reshape, 
                                         fparam, 
                                         natoms, 
                                         bias_atom_e = bias_atom_e, 
                                         reuse = reuse_weights, 
                                         suffix = suffix)

        if self.srtab is not None :
            sw_lambda, sw_deriv \
                = op_module.soft_min_switch(atype, 
                                            rij, 
                                            nlist,
                                            natoms,
                                            sel_a = self.sel_a,
                                            sel_r = self.sel_r,
                                            alpha = self.smin_alpha,
                                            rmin = self.sw_rmin,
                                            rmax = self.sw_rmax)            
            inv_sw_lambda = 1.0 - sw_lambda
            # NOTICE:
            # atom energy is not scaled, 
            # force and virial are scaled
            tab_atom_ener, tab_force, tab_atom_virial \
                = op_module.tab_inter(self.tab_info,
                                      self.tab_data,
                                      atype,
                                      rij,
                                      nlist,
                                      natoms,
                                      sw_lambda,
                                      sel_a = self.sel_a,
                                      sel_r = self.sel_r)
            energy_diff = tab_atom_ener - tf.reshape(atom_ener, [-1, natoms[0]])
            tab_atom_ener = tf.reshape(sw_lambda, [-1]) * tf.reshape(tab_atom_ener, [-1])
            atom_ener = tf.reshape(inv_sw_lambda, [-1]) * atom_ener
            energy_raw = tab_atom_ener + atom_ener
        else :
            energy_raw = atom_ener

        energy_raw = tf.reshape(energy_raw, [-1, natoms[0]], name = 'o_atom_energy'+suffix)
        energy = tf.reduce_sum(global_cvt_2_ener_float(energy_raw), axis=1, name='o_energy'+suffix)

        net_deriv_tmp = tf.gradients (atom_ener, descrpt_reshape)
        net_deriv = net_deriv_tmp[0]
        net_deriv_reshape = tf.reshape (net_deriv, [-1, natoms[0] * self.ndescrpt])

        force = op_module.prod_force_se_r (net_deriv_reshape,
                                            descrpt_deriv,
                                            nlist,
                                            natoms)
        if self.srtab is not None :
            sw_force \
                = op_module.soft_min_force(energy_diff, 
                                           sw_deriv,
                                           nlist, 
                                           natoms,
                                           n_a_sel = self.nnei_a,
                                           n_r_sel = self.nnei_r)
            force = force + sw_force + tab_force

        force = tf.reshape (force, [-1, 3 * natoms[1]], name = "o_force"+suffix)

        virial, atom_virial \
            = op_module.prod_virial_se_r (net_deriv_reshape,
                                           descrpt_deriv,
                                           rij,
                                           nlist,
                                           natoms)
        if self.srtab is not None :
            sw_virial, sw_atom_virial \
                = op_module.soft_min_virial (energy_diff,
                                             sw_deriv,
                                             rij,
                                             nlist,
                                             natoms,
                                             n_a_sel = self.nnei_a,
                                             n_r_sel = self.nnei_r)
            atom_virial = atom_virial + sw_atom_virial + tab_atom_virial
            virial = virial + sw_virial \
                     + tf.reduce_sum(tf.reshape(tab_atom_virial, [-1, natoms[1], 9]), axis = 1)

        virial = tf.reshape (virial, [-1, 9], name = "o_virial"+suffix)
        atom_virial = tf.reshape (atom_virial, [-1, 9 * natoms[1]], name = "o_atom_virial"+suffix)

        return energy, force, virial, energy_raw, atom_virial


    def build_atom_net (self, 
                        inputs,
                        fparam,
                        natoms,
                        bias_atom_e = None,
                        reuse = None,
                        suffix = '') :
        start_index = 0
        inputs = tf.reshape(inputs, [-1, self.ndescrpt * natoms[0]])
        shape = inputs.get_shape().as_list()
        if bias_atom_e is not None :
            assert(len(bias_atom_e) == self.ntypes)

        for type_i in range(self.ntypes):
            # cut-out inputs
            inputs_i = tf.slice (inputs,
                                 [ 0, start_index*      self.ndescrpt],
                                 [-1, natoms[2+type_i]* self.ndescrpt] )
            inputs_i = tf.reshape(inputs_i, [-1, self.ndescrpt])
            start_index += natoms[2+type_i]
            if bias_atom_e is None :
                type_bias_ae = 0.0
            else :
                type_bias_ae = bias_atom_e[type_i]

            # compute atom energy
            layer = self._filter_r(inputs_i, name='filter_r_type_'+str(type_i)+suffix, natoms=natoms, reuse=reuse, seed = self.seed)
            if self.numb_fparam > 0 :
                ext_fparam = tf.reshape(fparam, [-1, self.numb_fparam])
                ext_fparam = tf.tile(ext_fparam, [1, natoms[0]])
                ext_fparam = tf.reshape(ext_fparam, [-1, self.numb_fparam])
                layer = tf.concat([layer, ext_fparam], axis = 1)
            for ii in range(0,len(self.n_neuron)) :
                if ii >= 1 and self.n_neuron[ii] == self.n_neuron[ii-1] :
                    layer+= self.one_layer(layer, self.n_neuron[ii], name='layer_'+str(ii)+'_type_'+str(type_i)+suffix, reuse=reuse, seed = self.seed, use_timestep = self.resnet_dt)
                else :
                    layer = self.one_layer(layer, self.n_neuron[ii], name='layer_'+str(ii)+'_type_'+str(type_i)+suffix, reuse=reuse, seed = self.seed)
            final_layer = self.one_layer(layer, 1, activation_fn = None, bavg = type_bias_ae, name='final_layer_type_'+str(type_i)+suffix, reuse=reuse, seed = self.seed)
            final_layer = tf.reshape(final_layer, [-1, natoms[2+type_i]])
            # final_layer = tf.cond (tf.equal(natoms[2+type_i], 0), lambda: tf.zeros((0, 0), dtype=global_tf_float_precision), lambda : tf.reshape(final_layer, [-1, natoms[2+type_i]]))

            # concat the results
            if type_i == 0:
                outs = final_layer
            else:
                outs = tf.concat([outs, final_layer], axis = 1)

        return tf.reshape(outs, [-1])


    def _compute_dstats_sys_se_r (self,
                                  data_coord, 
                                  data_box, 
                                  data_atype,                             
                                  natoms_vec,
                                  mesh,
                                  reuse = None) :    
        avg_zero = np.zeros([self.ntypes,self.ndescrpt]).astype(global_np_float_precision)
        std_ones = np.ones ([self.ntypes,self.ndescrpt]).astype(global_np_float_precision)
        sub_graph = tf.Graph()
        with sub_graph.as_default():
            descrpt, descrpt_deriv, rij, nlist \
                = op_module.descrpt_se_r (tf.constant(data_coord),
                                           tf.constant(data_atype),
                                           tf.constant(natoms_vec, dtype = tf.int32),
                                           tf.constant(data_box),
                                           tf.constant(mesh),
                                           tf.constant(avg_zero),
                                           tf.constant(std_ones),
                                           rcut = self.rcut,
                                           rcut_smth = self.rcut_smth,
                                           sel = self.sel)
        # sub_sess = tf.Session(graph = sub_graph,
        #                       config=tf.ConfigProto(intra_op_parallelism_threads=self.run_opt.num_intra_threads, 
        #                                             inter_op_parallelism_threads=self.run_opt.num_inter_threads

        #                       ))
        sub_sess = tf.Session(graph = sub_graph)
        dd_all = sub_sess.run(descrpt)
        sub_sess.close()
        natoms = natoms_vec
        dd_all = np.reshape(dd_all, [-1, self.ndescrpt * natoms[0]])
        start_index = 0
        sysr = []
        sysa = []
        sysn = []
        sysr2 = []
        sysa2 = []
        for type_i in range(self.ntypes):
            end_index = start_index + self.ndescrpt * natoms[2+type_i]
            dd = dd_all[:, start_index:end_index]
            dd = np.reshape(dd, [-1, self.ndescrpt])
            start_index = end_index        
            # compute
            dd = np.reshape (dd, [-1, 1])
            ddr = dd[:,:1]
            sumr = np.sum(ddr)
            sumn = dd.shape[0]
            sumr2 = np.sum(np.multiply(ddr, ddr))
            sysr.append(sumr)
            sysn.append(sumn)
            sysr2.append(sumr2)
        return sysr, sysr2, sysn


    def _compute_std (self,sumv2, sumv, sumn) :
        return np.sqrt(sumv2/sumn - np.multiply(sumv/sumn, sumv/sumn))

    def _filter_r(self, 
                  inputs, 
                  natoms,
                  activation_fn=tf.nn.tanh, 
                  stddev=1.0,
                  bavg=0.0,
                  name='linear', 
                  reuse=None,
                  seed=None):
        # natom x nei
        shape = inputs.get_shape().as_list()
        outputs_size = [1] + self.filter_neuron
        with tf.variable_scope(name, reuse=reuse):
            start_index = 0
            xyz_scatter_total = []
            for type_i in range(self.ntypes):
                # cut-out inputs
                # with natom x nei_type_i
                inputs_i = tf.slice (inputs,
                                     [ 0, start_index       ],
                                     [-1, self.sel_r[type_i]] )
                start_index += self.sel_r[type_i]
                shape_i = inputs_i.get_shape().as_list()
                # with (natom x nei_type_i) x 1
                xyz_scatter = tf.reshape(inputs_i, [-1, 1])
                for ii in range(1, len(outputs_size)):
                    w = tf.get_variable('matrix_'+str(ii)+'_'+str(type_i), 
                                        [outputs_size[ii - 1], outputs_size[ii]], 
                                        global_tf_float_precision,
                                        tf.random_normal_initializer(stddev=stddev/np.sqrt(outputs_size[ii]+outputs_size[ii-1]), seed = seed))
                    b = tf.get_variable('bias_'+str(ii)+'_'+str(type_i), 
                                        [1, outputs_size[ii]], 
                                        global_tf_float_precision,
                                        tf.random_normal_initializer(stddev=stddev, mean = bavg, seed = seed))
                    if self.filter_resnet_dt :
                        idt = tf.get_variable('idt_'+str(ii)+'_'+str(type_i), 
                                              [1, outputs_size[ii]], 
                                              global_tf_float_precision,
                                              tf.random_normal_initializer(stddev=0.001, mean = 1.0, seed = seed))
                    if outputs_size[ii] == outputs_size[ii-1]:
                        if self.filter_resnet_dt :
                            xyz_scatter += activation_fn(tf.matmul(xyz_scatter, w) + b) * idt
                        else :
                            xyz_scatter += activation_fn(tf.matmul(xyz_scatter, w) + b)
                    elif outputs_size[ii] == outputs_size[ii-1] * 2: 
                        if self.filter_resnet_dt :
                            xyz_scatter = tf.concat([xyz_scatter,xyz_scatter], 1) + activation_fn(tf.matmul(xyz_scatter, w) + b) * idt
                        else :
                            xyz_scatter = tf.concat([xyz_scatter,xyz_scatter], 1) + activation_fn(tf.matmul(xyz_scatter, w) + b)
                    else:
                        xyz_scatter = activation_fn(tf.matmul(xyz_scatter, w) + b)
                # natom x nei_type_i x out_size
                xyz_scatter = tf.reshape(xyz_scatter, (-1, shape_i[1], outputs_size[-1]))
                xyz_scatter_total.append(xyz_scatter)

            # natom x nei x outputs_size
            xyz_scatter = tf.concat(xyz_scatter_total, axis=1)
            # natom x outputs_size
            # 
            res_rescale = 1./5.
            result = tf.reduce_mean(xyz_scatter, axis = 1) * res_rescale

        return result

