import torch
from torch import nn
from torch import Tensor

from models.deepBspline import DeepBSpline
from models.deepBspline_explicit_linear import DeepBSplineExplicitLinear
from models.deepRelu import DeepReLU
from ds_utils import spline_grid_from_range


class BaseModel(nn.Module):

    def __init__(self, **params):
        """ """
        super().__init__()

        self.params = params

        self.set_attributes('activation_type', 'dataset_name',
                            'num_classes', 'device')
        # deepspline
        self.set_attributes('spline_init', 'spline_size',
                            'spline_range', 'slope_diff_threshold')

        self.spline_grid = spline_grid_from_range(self.spline_size,
                                                    self.spline_range)

        self.deepspline = None
        if self.activation_type == 'deepRelu':
            self.deepspline = DeepReLU
        elif self.activation_type == 'deepBspline':
            self.deepspline = DeepBSpline
        elif self.activation_type == 'deepBspline_explicit_linear':
            self.deepspline = DeepBSplineExplicitLinear


    def set_attributes(self, *names):
        """ """
        for name in names:
            assert isinstance(name, str), f'{name} is not string.'
            if name in self.params:
                setattr(self, name, self.params[name])


    ############################################################################
    # Activation initialization


    def init_activation_list(self, activation_specs, bias=True, **kwargs):
        """ Initialize list of activations

        Args:
            activation_specs : list of pairs ('layer_type', num_channels/neurons);
                                len(activation_specs) = number of activation layers;
                                e.g., [('conv', 64), ('linear', 100)].
            bias : explicit bias; only relevant for explicit_linear activations.
        """
        assert isinstance(activation_specs, list)

        if self.deepspline is not None:
            size, grid = self.spline_size, self.spline_grid
            activations = nn.ModuleList()
            for mode, num_activations in activation_specs:
                activations.append(self.deepspline(size=size, grid=grid, init=self.spline_init,
                                            bias=False, mode=mode, num_activations=num_activations, #bias=bias
                                            device=self.device))
        else:
            activations = self.init_standard_activations(activation_specs)

        return activations



    def init_activation(self, activation_specs, **kwargs):
        """ Initialize a single activation

        Args:
            activation_specs: tuple, e.g., ('conv', 64)
        """
        assert isinstance(activation_specs, tuple)
        activation = self.init_activation_list([activation_specs], **kwargs)[0]

        return activation



    def init_standard_activations(self, activation_specs, **kwargs):
        """ Initialize non-spline activations and puts them in nn.ModuleList()

        Args:
            activation_type : 'relu', 'leaky_relu'.
            activation_specs : list of pairs ('layer_type', num_channels/neurons);
                                len(activation_specs) = number of activation layers.
        """
        activations = nn.ModuleList()

        if self.activation_type == 'relu':
            relu = nn.ReLU()
            for i in range(len(activation_specs)):
                activations.append(relu)

        elif self.activation_type == 'leaky_relu':
            leaky_relu = nn.LeakyReLU()
            for i in range(len(activation_specs)):
                activations.append(leaky_relu)

        else:
            raise ValueError(f'{self.activation_type} is not in relu family...')

        return activations



    def initialization(self, init_type='He'):
        """ """
        assert init_type in ['He', 'Xavier', 'custom_normal']

        if init_type == 'He':
            if self.activation_type in ['leaky_relu', 'relu']:
                nonlinearity = self.activation_type
                slope_init = 0.01 if nonlinearity == 'leaky_relu' else 0.

            elif self.deepspline is not None and self.spline_init in ['leaky_relu', 'relu']:
                nonlinearity = self.spline_init
                slope_init = 0.01 if nonlinearity == 'leaky_relu' else 0.
            else:
                init_type = 'Xavier' # overwrite init_type


        for module in self.modules():

            if isinstance(module, nn.Conv2d):
                if init_type == 'Xavier':
                    nn.init.xavier_normal_(module.weight)

                elif init_type == 'custom_normal':
                    # custom Gauss(0, 0.05) weight initialization
                    module.weight.data.normal_(0, 0.05)
                    module.bias.data.zero_()

                else: # He initialization
                    nn.init.kaiming_normal_(module.weight, a=slope_init, mode='fan_out',
                                            nonlinearity=nonlinearity)

            elif isinstance(module, nn.BatchNorm2d):
                module.weight.data.fill_(1)
                module.bias.data.zero_()


    ###########################################################################
    # Parameters


    def get_num_params(self):
        """ """
        num_params = 0
        for param in self.parameters():
            num_params += torch.numel(param)

        return num_params


    def modules_deepspline(self):
        """ """
        for module in self.modules():
            if isinstance(module, self.deepspline):
                yield module


    def named_parameters_no_deepspline(self, recurse=True):
        """ Named parameters of network, excepting deepspline parameters.
        """
        try:
            for name, param in self.named_parameters(recurse=recurse):
                deepspline_param = False
                # get all deepspline parameters
                if self.deepspline is not None:
                    for param_name in self.deepspline.parameter_names():
                        if name.endswith(param_name):
                            deepspline_param = True

                if deepspline_param is False:
                    yield name, param

        except AttributeError:
            print('Not using deepspline activations...')
            raise



    def named_parameters_deepspline(self, recurse=True):
        """ Named parameters (for optimizer) of deepspline activations.
        """
        try:
            for name, param in self.named_parameters(recurse=recurse):
                deepspline_param = False
                for param_name in self.deepspline.parameter_names():
                    if name.endswith(param_name):
                        deepspline_param = True

                if deepspline_param is True:
                    yield name, param

        except AttributeError:
            print('Not using deepspline activations...')
            raise



    def parameters_no_deepspline(self):
        """ """
        for name, param in self.named_parameters_no_deepspline(recurse=True):
            yield param



    def parameters_deepspline(self):
        """ """
        for name, param in self.named_parameters_deepspline(recurse=True):
            yield param



    def parameters_batch_norm(self):
        """ """
        for module in self.modules():
            if isinstance(module, nn.BatchNorm2d):
                yield module.weight, module.bias



    def freeze_parameters(self):
        for param in self.parameters():
            param.requires_grad = False


    ############################################################################
    # Deepsplines: regularization and sparsification

    @property
    def weight_decay_regularization(self):
        """ boolean """
        return (self.params['weight_decay'] > 0)


    @property
    def tv_bv_regularization(self):
        """ boolean """
        return (self.deepspline is not None and self.params['lmbda'] > 0)



    def weight_decay(self):
        """ Computes the total weight decay of the network.

        For the resnet, also apply weight decay with a fixed
        value to the batchnorm weights and biases.
        Note: Fixed weight decay is also applied to the explicit linear
        parameters, if using DeepBSplineExplicitLinear activation.
        """
        wd = Tensor([0.]).to(self.device)

        for module in self.modules():
            if hasattr(module, 'weight') and isinstance(module.weight, nn.Parameter):
                wd = wd + self.params['weight_decay']/2 * module.weight.pow(2).sum()

            if hasattr(module, 'bias') and isinstance(module.bias, nn.Parameter):
                wd = wd + self.params['weight_decay']/2 * module.bias.pow(2).sum()

        return wd[0] # 1-element 1d tensor -> 0d tensor



    def TV_BV(self):
        """ Computes the sum of the TV(2)/BV(2) norm of all activations

        Returns:
            BV(2), if lipschitz is True;
            TV(2), if lipschitz is False.
        """
        tv_bv = Tensor([0.]).to(self.device)
        tv_bv_unweighted = Tensor([0.]).to(self.device) # for printing loss without weighting

        for module in self.modules():
            if isinstance(module, self.deepspline):
                module_tv_bv = module.totalVariation(mode='additive')
                if self.params['lipschitz'] is True:
                    module_tv_bv = module_tv_bv + module.fZerofOneAbs(mode='additive')

                tv_bv = tv_bv + self.params['lmbda'] * module_tv_bv.norm(p=1)
                with torch.no_grad():
                    tv_bv_unweighted = tv_bv_unweighted + module_tv_bv.norm(p=1)

        return tv_bv[0], tv_bv_unweighted[0] # 1-element 1d tensor -> 0d tensor



    def lipschitz_bound(self):
        """ Returns the lipschitz bound of the network

        The lipschitz bound associated with C is:
        ||f_deep(x_1) - f_deep(x_2)||_1 <= C ||x_1 - x_2||_1,
        for all x_1, x_2 \in R^{N_0}.

        For l \in {1, ..., L}, n \in {1,..., N_l}:
        w_{n, l} is the vector of weights from layer l-1 to layer l, neuron n;
        s_{n, l} is the activation function of layer l, neuron n.

        C = (prod_{l=1}^{L} [max_{n,l} w_{n,l}]) * (prod_{l=1}^{L} ||s_l||_{BV(2)}),
        where ||s_l||_{BV(2)} = sum_{n=1}^{N_l} {TV(2)(s_{n,l}) + |s_{n,l}(0)| + |s_{n,l}(1)|}.

        For details, please see https://arxiv.org/pdf/2001.06263v1.pdf
        (Theorem 1, with p=1, q=\infty).
        """
        bv_product = Tensor([1.]).to(self.device)
        max_weights_product = Tensor([1.]).to(self.device)

        for module in self.modules():
            if isinstance(module, self.deepspline):
                module_tv = module.totalVariation()
                module_fzero_fone = module.fZerofOneAbs()
                bv_product = bv_product * (module_tv.sum() + module_fzero_fone.sum())

            elif isinstance(module, nn.Linear) or isinstance(module, nn.Conv2d):
                max_weights_product = max_weights_product * module.weight.data.abs().max()

        lip_bound = max_weights_product * bv_product

        return lip_bound[0] # 1-element 1d tensor -> 0d tensor



    def sparsify_activations(self):
        """ Sparsifies the deepspline activations, eliminating the slope
        changes smaller than a threshold.

        Note that deepspline(x) = sum_k [a_k * ReLU(x-kT)] + (b1*x + b0)
        This function sets a_k to zero if |a_k| < slope_diff_threshold.
        """
        for module in self.modules():
            if isinstance(module, self.deepspline):
                module.apply_threshold(self.slope_diff_threshold)


    def compute_sparsity(self):
        """ Returns the sparsity of the activations (see deepspline.py)
        """
        sparsity = 0
        for module in self.modules():
            if isinstance(module, self.deepspline):
                module_sparsity, _ = module.get_threshold_sparsity(self.slope_diff_threshold)
                sparsity += module_sparsity.sum().item()

        return sparsity



    def get_deepspline_activations(self):
        """ Returns a list of activation parameters for each deepspline activation layer.
        """
        with torch.no_grad():
            activations_list = []
            for name, module in self.named_modules():

                if isinstance(module, self.deepspline):
                    grid_tensor = module.grid_tensor # (num_activations, size)
                    input = grid_tensor.transpose(0,1) # (size, num_activations)
                    if module.mode == 'conv':
                        input = input.unsqueeze(-1).unsqueeze(-1) # 4D

                    output = module(input)
                    output = output.transpose(0, 1)
                    if module.mode == 'conv':
                        # (num_activations, size)
                        output = output.squeeze(-1).squeeze(-1)

                    _, threshold_sparsity_mask = module.get_threshold_sparsity(self.slope_diff_threshold)
                    activations_list.append({'name': '_'.join([name, module.mode]),
                                            'x': grid_tensor.clone().detach().cpu(),
                                            'y': output.clone().detach().cpu(),
                                            'threshold_sparsity_mask' : threshold_sparsity_mask.cpu()})

        return activations_list
