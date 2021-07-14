"""
This module provides the base class for deepBspline activation functions.

A linear spline activation with parameters {a_k} and b1, b0, with knots placed
on a grid of spacing T can be represented as:
deepspline(x) = sum_k [a_k * ReLU(x-kT)] + (b1*x + b0)

The ReLU representation is not well-conditioned and leads to an exponential growth
with the number of coefficients of the computational and memory requirements for
training the network.
In this module, we use an alternative B1-spline representation for the activations.
the number of B-spline coefficients exceeds the number of ReLU coefficients by 2,
such that len(a) + len((b1, b_0)) = len(c), so as have the same total amount of
parameters.

The coefficients of the ReLU can be computed via:
a = Lc, where L is a second finite difference matrix.

This additional number of B1 spline coefficients (2), compared to the ReLU,
allows the unique specification of the linear term term, which is in the
nullspace of the L second finite-difference matrix.
In other words, two sets of coefficients [c], [c'] which are related by
a linear term, give the same ReLU coefficients [a].
Outside a region of interest, the activation is computed via left and right
linear extrapolations using the two leftmost and rightmost coefficients,
respectively.

The regularization term applied to this function is:
TV(2)(deepsline) = ||a||_1 = ||Lc||_1
"""

import torch
from torch import nn
from torch.nn import functional as F
from torch import Tensor
import numpy as np
from scipy.linalg import toeplitz
from abc import abstractproperty
from models.deepspline_base import DeepSplineBase


class DeepBSpline_Func(torch.autograd.Function):
    """
    Autograd function to only backpropagate through the B-splines that were used
    to calculate output = activation(input), for each element of the input.

    If save_memory=True, use a memory efficient version at the expense of
    some additional running time.
    """

    @staticmethod
    def forward(ctx, x, coefficients_vect, grid, zero_knot_indexes, size, save_memory):

        # First, we clamp the input to the range [leftmost coefficient, second righmost coefficient].
        # We have to clamp, on the right, to the second righmost coefficient, so that we always have
        # a coefficient to the right of x_clamped to compute its output.
        # For the values outside the range, linearExtrapolations will add what remains
        # to compute the final output of the activation, taking into account the slopes
        # on the left and right.
        x_clamped = x.clamp(min = -(grid.item() * (size//2)),
                            max = (grid.item() * (size//2-1)))

        floored_x = torch.floor(x_clamped/grid) # left coefficient
        fracs = x_clamped/grid - floored_x # distance to left coefficient

        # This gives the indexes (in coefficients_vect) of the left coefficients
        indexes=(zero_knot_indexes.view(1, -1, 1, 1) + floored_x).long()

        # Only two B-spline basis functions are required to compute the output
        # (through linear interpolation) for each input in the B-spline range.
        activation_output = coefficients_vect[indexes+1]*fracs + \
                            coefficients_vect[indexes]*(1-fracs)

        if save_memory is False:
            ctx.save_for_backward(save_memory, fracs, coefficients_vect, indexes, grid)
        else:
            ctx.save_for_backward(save_memory, x, coefficients_vect, grid, zero_knot_indexes, size)

            # compute leftmost and rightmost slopes for linear extrapolations outside B-spline range
            num_activations = x.size(1)
            coefficients = coefficients_vect.view(num_activations, size)
            leftmost_slope = (coefficients[:,1] - coefficients[:,0]).div(grid).view(1,-1,1,1)
            rightmost_slope = (coefficients[:,-1] - coefficients[:,-2]).div(grid).view(1,-1,1,1)

            # peform linear extrapolations outside B-spline range
            leftExtrapolations  = (x.detach() + grid*(size//2)).clamp(max=0) * leftmost_slope
            rightExtrapolations = (x.detach() - grid*(size//2-1)).clamp(min=0) * rightmost_slope
            # linearExtrapolations is zero for inputs inside B-spline range
            linearExtrapolations = leftExtrapolations + rightExtrapolations

            # add linear extrapolations to B-spline expansion
            activation_output = activation_output + linearExtrapolations


        return activation_output


    @staticmethod
    def backward(ctx, grad_out):

        save_memory = ctx.saved_tensors[0]

        if save_memory is False:
            fracs, coefficients_vect, indexes, grid = ctx.saved_tensors[1:]
        else:
            x, coefficients_vect, grid, zero_knot_indexes, size = ctx.saved_tensors[1:]

            # compute fracs and indexes again (do not save them in ctx) to save memory
            x_clamped = x.clamp(min = -(grid.item() * (size//2)),
                                max = (grid.item() * (size//2-1)))

            floored_x = torch.floor(x_clamped/grid) # left coefficient
            fracs = x_clamped/grid - floored_x # distance to left coefficient

            # This gives the indexes (in coefficients_vect) of the left coefficients
            indexes=(zero_knot_indexes.view(1, -1, 1, 1) + floored_x).long()


        grad_x = (coefficients_vect[indexes+1] - coefficients_vect[indexes]) / grid * grad_out

        # Next, add the gradients with respect to each coefficient, such that,
        # for each data point, only the gradients wrt to the two closest
        # coefficients are added (since only these can be nonzero).

        grad_coefficients_vect = torch.zeros_like(coefficients_vect)
        # right coefficients gradients
        grad_coefficients_vect.scatter_add_(0, indexes.view(-1)+1, (fracs*grad_out).view(-1))
        # left coefficients gradients
        grad_coefficients_vect.scatter_add_(0, indexes.view(-1), ((1-fracs)*grad_out).view(-1))

        if save_memory is True:
            # Add gradients from the linear extrapolations
            tmp1 = ((x.detach() + grid*(size//2)).clamp(max=0))/grid
            grad_coefficients_vect.scatter_add_(0, indexes.view(-1), (-tmp1*grad_out).view(-1))
            grad_coefficients_vect.scatter_add_(0, indexes.view(-1)+1, (tmp1*grad_out).view(-1))

            tmp2 = ((x.detach() - grid*(size//2-1)).clamp(min=0))/grid
            grad_coefficients_vect.scatter_add_(0, indexes.view(-1), (-tmp2*grad_out).view(-1))
            grad_coefficients_vect.scatter_add_(0, indexes.view(-1)+1, (tmp2*grad_out).view(-1))


        return grad_x, grad_coefficients_vect, None, None, None, None



class DeepBSplineBase(DeepSplineBase):
    """
    Parent class for DeepBSpline activations (deepBspline/deepBspline_explicit_Linear)
    """

    def __init__(self, save_memory=False, **kwargs):
        """
        Args:
            save_memory (bool):
                weather to use a more memory efficient version (takes more time).
        """

        super().__init__(**kwargs)

        self.save_memory = save_memory
        self.init_zero_knot_indexes()
        self.init_derivative_filters()



    def init_zero_knot_indexes(self):
        """ Initialize indexes of zero knots of each activation.
        """
        # self.zero_knot_indexes[i] gives index of knot 0 for filter/neuron_i.
        # size: (num_activations,)
        activation_arange = torch.arange(0, self.num_activations).to(**self.device_type)
        self.zero_knot_indexes = (activation_arange*self.size + (self.size//2))



    def init_derivative_filters(self):
        """ Initialize D1, D2 filters.
        """
        # Derivative filters
        self.D1_filter = Tensor([-1,1]).view(1,1,2).to(**self.device_type).div(self.grid)
        self.D2_filter = Tensor([1,-2,1]).view(1,1,3).to(**self.device_type).div(self.grid)



    @property
    def grid_tensor(self):
        """
        Locations of B-spline coefficients.
        """
        return self.get_grid_tensor(self.size, self.grid)


    def get_grid_tensor(self, size_, grid_):
        """
        Creates a 2D grid tensor of size (num_activations, size)
        with the locations of the B-spline coefficients.

        Args:
            size (odd int): number of B-spline coefficients
            grid (float): spacing between B-spline basis elements.
        """
        grid_arange = torch.arange(-(size_//2),
                                    (size_//2)+1).to(**self.device_type).mul(grid_)
        grid_tensor = grid_arange.expand((self.num_activations, size_))

        return grid_tensor



    @abstractproperty
    def coefficients_vect_(self):
        """ B-spline vectorized coefficients of activations """
        pass


    @property
    def coefficients(self):
        """ B-spline coefficients.
        """
        return self.coefficients_vect_.view(self.num_activations, self.size)


    @property
    def relu_slopes(self):
        """ Get the activation relu slopes {a_k},
        by doing a valid convolution of the coefficients {c_k}
        with the second-order finite-difference filter [1,-2,1].
        """
        # F.conv1d():
        # out(i, 1, :) = self.D2_filter(1, 1, :) *conv* coefficients(i, 1, :)
        # out.size() = (num_activations, 1, filtered_activation_size)
        # after filtering, we remove the singleton dimension
        return F.conv1d(self.coefficients.unsqueeze(1), self.D2_filter).squeeze(1)



    def forward(self, input):
        """
        Args:
            input (torch.Tensor):
                2D or 4D, depending on weather the layer is
                convolutional ('conv') or fully-connected ('fc')

        Returns:
            output (torch.Tensor)
        """
        input_size = input.size()
        x = self.reshape_forward(input)

        assert x.size(1) == self.num_activations, 'input.size(1) != num_activations.'

        output = DeepBSpline_Func.apply(x, self.coefficients_vect_, self.grid,
                                        self.zero_knot_indexes, self.size, self.save_memory)

        if self.save_memory is False:
            # Linear extrapolations:
            # f(x_left) = leftmost coeff value + left_slope * (x - leftmost coeff)
            # f(x_right) = second rightmost coeff value + right_slope * (x - second rightmost coeff)
            # where the first components of the sums (leftmost/second rightmost coeff value)
            # are taken into account in DeepBspline_Func() and linearExtrapolations adds the rest.

            coefficients = self.coefficients
            leftmost_slope = (coefficients[:,1] - coefficients[:,0]).div(self.grid).view(1,-1,1,1)
            rightmost_slope = (coefficients[:,-1] - coefficients[:,-2]).div(self.grid).view(1,-1,1,1)

            # x.detach(): gradient w/ respect to x is already tracked in DeepBSpline_Func
            leftExtrapolations  = (x.detach() + self.grid*(self.size//2)).clamp(max=0) * leftmost_slope
            rightExtrapolations = (x.detach() - self.grid*(self.size//2-1)).clamp(min=0) * rightmost_slope
            # linearExtrapolations is zero for inputs inside B-spline range
            linearExtrapolations = leftExtrapolations + rightExtrapolations

            output = output + linearExtrapolations


        output = self.reshape_back(output, input_size)

        return output



    def apply_threshold(self, threshold):
        """
        Applies a threshold to the activations, eliminating the relu
        slopes smaller than a threshold and updates the B-spline coefficients.

        Operations performed:
        . [a] = L[c], [a] -> sparsification -> [a_hat];
        . [c_hat] = f([a], c_{-L}, c_{-L+1}).

        This uses a well-conditioned iterative method to convert from the
        deeprelu representation to the B-spline representation
        (see iterative_relu_slopes_to_coefficients()).
        This is required so that we can do (b0,b1,[a])->[c]->[a']
        while keeping the same zero slopes in [a] and [a'].
        This is not the case if we compute [a'] = L(P(b0,b1,[a])), where
        P is a matrix that maps (b0,b1,[a]) to [c], due to ill-conditioning.

        Args:
            threshold (float)
        """
        with torch.no_grad():
            new_relu_slopes = super().apply_threshold(threshold)
            self.coefficients_vect_.data = \
                self.iterative_relu_slopes_to_coefficients(new_relu_slopes).view(-1)



    def iterative_relu_slopes_to_coefficients(self, relu_slopes):
        """
        Get the (B-spline) coefficients from relu coefficients in an
        iterative and well-conditioned manner.

        Operations performed:
        . [c_hat] = f([a], c_{-L}, c_{-L+1}).

        When converting from a to c, we need to use the additional information
        provided by the the first two B-spline coefficients, which are kept
        fixed. This additional information determines the linear term parameters
        (b0, b1), which are lost when doing a = Lc, since two sets of
        coefficients ([c], [c']) that are related by a linear term give
        the same ReLU coefficients [a].

        Args:
            relu_slopes (torch.Tensor)
        """
        coefficients = self.coefficients
        coefficients[:, 2::] = 0. # first two coefficients remain the same

        for i in range(2, self.size):
            coefficients[:, i] = (coefficients[:, i-1] - coefficients[:, i-2]) + \
                                    relu_slopes[:, i-2].mul(self.grid) + coefficients[:, i-1]

        return coefficients
