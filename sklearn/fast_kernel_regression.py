import numpy as np
import scipy as sp
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.metrics.pairwise import pairwise_kernels, euclidean_distances
from sklearn.utils import check_random_state
from sklearn.utils.validation import check_is_fitted, check_X_y


class FastKernelRegression(RegressorMixin, BaseEstimator):
    """Fast kernel regression.

    Train least squared kernel regression model with mini-batch EigenPro
    iteration.

    Parameters
    ----------
        bs: int, default = 'auto'
            Mini-batch size for gradient descent.

        n_epoch : int, default = 1
            The number of passes over the training data.

        n_components : int, default = 1000
            the maximum number of eigendirections used in modifying the kernel
            operator. Convergence rate speedup over normal gradient descent is
            approximately the largest eigenvalue over the n_componentth
            eigenvalue, however, it may take time to compute eigenvalues for
            large n_components

        subsample_size : int, default = 'auto'
            The number of subsamples used for estimating the largest n_component
            eigenvalues and eigenvectors. When it is set to 'auto', it will be
            4000 if there are less than 100,000 samples (for training),
            and otherwise 10000.

        mem_gb : int, default = 12
            Physical device memory in GB.

        kernel : string or callable, default = "gaussian"
            Kernel mapping used internally. Strings can be anything supported
            by sklearn's library, however, it is recommended to use a radial
            kernel. There is special support for gaussian, laplace, and cauchy
            kernels. A callable should accept two arguments and return a
            floating point number.

        bandwidth : float, default=5
            Bandwidth to use with the gaussian, laplacian, and cauchy kernels.
            Ignored by other kernels.

        gamma : float, default=None
            Gamma parameter for the RBF, polynomial, exponential chi2 and
            sigmoid kernels. Interpretation of the default value is left to
            the kernel; see the documentation for sklearn.metrics.pairwise.
            Ignored by other kernels.

        degree : float, default=3
            Degree of the polynomial kernel. Ignored by other kernels.

        coef0 : float, default=1
            Zero coefficient for polynomial and sigmoid kernels.
            Ignored by other kernels.

        kernel_params : mapping of string to any, optional
            Additional parameters (keyword arguments) for kernel function
            passed as callable object.

        random_state : int
            The random seed to be used. This class uses np.random for number
            generation.

       References
       ----------
       * Siyuan Ma, Mikhail Belkin
         "Diving into the shallows: a computational perspective on
         large-scale machine learning", NIPS 2017.

       Examples
       --------
           >>> from sklearn.fast_kernel_regression import FastKernelRegression
           >>> import numpy as np
           >>> n_samples, n_features, n_targets = 4000, 20, 3
           >>> rng = np.random.RandomState(1)
           >>> x_train = rng.randn(n_samples, n_features)
           >>> y_train = rng.randn(n_samples, n_targets)
           >>> rgs = FastKernelRegression(n_epoch=3, bandwidth=1)
           >>> rgs.fit(x_train, y_train)
           FastKernelRegression(bandwidth=1, bs='auto', coef0=1, degree=3, gamma=None,
                      kernel='gaussian', kernel_params=None, mem_gb=12,
                      n_components=1000, n_epoch=3, random_state=None,
                      subsample_size='auto')
           >>> y_pred = rgs.predict(x_train)
           >>> loss = np.mean(np.square(y_train - y_pred))
    """

    def __init__(self, bs="auto", n_epoch=1, n_components=1000,
                 subsample_size="auto", mem_gb=12, kernel="gaussian",
                 bandwidth=5, gamma=None, degree=3, coef0=1,
                 kernel_params=None, random_state=None):
        self.bs = bs
        self.n_epoch = n_epoch
        self.n_components = n_components
        self.subsample_size = subsample_size
        self.mem_gb = mem_gb
        self.kernel = kernel
        self.bandwidth = bandwidth
        self.gamma = gamma
        self.degree = degree
        self.coef0 = coef0
        self.kernel_params = kernel_params
        self.random_state = random_state

    def _kernel(self, X, Y, Y_squared=None):
        """Calculate the kernel matrix

        Parameters
        ---------
        X : {float, array}, shape = [n_samples, n_features]
            Input data.

        Y : {float, array}, shape = [n_centers, n_targets]
            Kernel centers.

        Y_squared : {float, array}, shape = [1, n_centers]
            Square of L2 norms of centers.

        Returns
        -------
        K : {float, array}, shape = [n_samples, n_centers]
            Kernel matrix.
        """
        if (self.kernel != "gaussian" and
                self.kernel != "laplace" and
                self.kernel != "cauchy"):
            if callable(self.kernel):
                params = self.kernel_params or {}
            else:
                params = {"gamma": self.gamma,
                          "degree": self.degree,
                          "coef0": self.coef0}
            return pairwise_kernels(X, Y, metric=self.kernel,
                                    filter_params=True, **params)
        distance = euclidean_distances(X, Y, squared=True,
                                       Y_norm_squared=Y_squared)
        bandwidth = np.float32(self.bandwidth)
        if self.kernel == "gaussian":
            shape = -1 / (2 * (np.square(bandwidth)))
            K = np.exp(distance * shape)
        elif self.kernel == "laplace":
            d = np.maximum(distance, 0)
            K = np.exp(-np.sqrt(d) / bandwidth)
        elif self.kernel == "cauchy":
            K = 1 / (1 + distance / np.square(bandwidth))
        return K

    def _nystrom_svd(self, X, n_samples, n_components):
        """Compute the top eigensystem of a kernel operator using Nystrom method

        Parameters
        ----------
        X : {float, array}, shape = [n_subsamples, n_features]
            Subsample feature matrix.

        n_samples : float
            Number of total samples.

        n_components : int
            Number of top eigencomponents to be restored.

        Returns
        -------
        S : {float, array}, shape = [k]
            Top eigenvalues.

        V : {float, array}, shape = [n_subsamples, k]
            Top eigenvectors of a subsample kernel matrix (which can be
            directly used to approximate the eigenfunctions of the kernel
            operator).
        """
        m, _ = X.shape
        K = self._kernel(X, X)
        W = K * (np.float32(n_samples) / m)
        S, V = sp.linalg.eigh(W, eigvals=(m - n_components, m - 1))

        # Flip so eigenvalues are in descending order.
        S = np.maximum(np.float32(1e-7), np.flipud(S))
        V = np.fliplr(V)

        return S, V[:, :n_components] * (np.sqrt(np.float32(n_samples) / m))

    def _setup(self, feat, max_components, n_samples, mG, alpha):
        """Compute preconditioner and scale factors for EigenPro iteration

        Parameters
        ----------
        feat : {float, array}, shape = [n_samples, n_features]
            Feature matrix (normally from training data).

        max_components : float
            Maximum number of components to be used in EigenPro iteration.

        n_samples : int
            Number of total samples.

        mG : int
            Maximum batch size to fit in memory.

        alpha : float
            Exponential factor (< 1) for eigenvalue ratio.

        Returns
        -------
        max_S : float
            Normalized largest eigenvalue.

        max_kxx : float
            Maximum of k(x,x) where k is the EigenPro kernel.
        """
        alpha = np.float32(alpha)

        # Estimate eigenvalues (S) and eigenvectors (V) of the kernel matrix
        # corresponding to the feature matrix.
        S, V = self._nystrom_svd(feat, n_samples, max_components)
        n_subsamples = feat.shape[0]

        # Calculate the number of components to be used such that the
        # corresponding batch size is bounded by the subsample size and the
        # memory size.
        n_components = np.sum(np.float32(n_samples) / S <
                              min(n_subsamples, mG)) - 1
        self.V_ = V[:, :n_components]

        scale = np.power(S[0] / S[n_components], alpha)

        # Compute part of the preconditioner for step 2 of gradient descent in
        # the eigenpro model
        self.Q_ = (1 - np.power(S[n_components] / S[:n_components],
                                alpha)) / S[:n_components]

        max_S = (S[0] / n_samples).astype(np.float32)
        kxx = 1 - np.sum(self.V_ ** 2, axis=1) * n_subsamples / n_samples
        return max_S / scale, np.max(kxx)

    def _initialize_params(self, X, Y):
        """Validate parameters passed to the model, choose parameters
        that have not been passed in, and run setup for EigenPro iteration.
        """
        self.random_state_ = check_random_state(self.random_state)
        n, d = X.shape
        n_label = 1 if len(Y.shape) == 1 else Y.shape[1]
        self.centers_ = X

        # Calculate the subsample size to be used.
        if self.subsample_size is "auto":
            if n < 100000:
                sample_size = min(n, 4000)
            else:
                sample_size = 10000
        else:
            sample_size = min(n, self.subsample_size)

        n_components = min(sample_size - 1, self.n_components)
        n_components = max(1, n_components)

        mem_bytes = self.mem_gb * 1024 ** 3 - 100 * 1024 ** 2  # preserve 100MB
        mem_usages = (d + n_label + 3 * np.arange(sample_size)) * n * 4
        mG = np.sum(mem_usages < mem_bytes)

        # Calculate largest eigenvalue and max{k(x,x)} using subsamples.
        self.pinx_ = self.random_state_.choice(n, sample_size,
                                               replace=False).astype('int32')
        max_S, beta = self._setup(X[self.pinx_], n_components, n, mG, .9)

        # Calculate best batch size.
        if self.bs is "auto":
            self.bs_ = np.int32(beta / max_S + 1)
        else:
            self.bs_ = self.bs
        self.bs_ = min(self.bs_, n)

        # Calculate best step size.
        if self.bs_ < beta / max_S + 1:
            self.eta_ = self.bs_ / beta
        elif self.bs_ < n:
            self.eta_ = 2. * self.bs_ / (beta + (self.bs_ - 1) *
                                         max_S)
        else:
            self.eta_ = 0.95 * 2 / max_S
        self.eta_ = np.float32(self.eta_)

        # Remember the shape of Y for predict() and ensure it's shape is 2-D.
        self.was_1D_ = False
        if len(Y.shape) == 1:
            Y = np.reshape(Y, (Y.shape[0], 1))
            self.was_1D_ = True
        return Y

    def fit(self, X, Y):
        """Train fast kernel regression model

        Parameters
        ----------
        X : {float, array}, shape = [n_samples, n_features]
            Training data.

        Y : {float, array}, shape = [n_samples, n_targets]
            Training targets.

        Returns
        -------
        self : returns an instance of self.
        """
        X, Y = check_X_y(X, Y, dtype=np.float32, multi_output=True,
                         ensure_min_samples=3, y_numeric=True)
        Y = Y.astype(np.float32)  # check_X_y does not seem to do this
        """Parameter Initialization"""
        Y = self._initialize_params(X, Y)

        """Training loop"""
        n = self.centers_.shape[0]

        self.coef_ = np.zeros((n, Y.shape[1]), dtype=np.float32)
        self.centers_squared_ = \
            np.square(self.centers_).sum(axis=1, keepdims=True).T
        step = np.float32(self.eta_ / self.bs_)
        for epoch in range(0, self.n_epoch):
            epoch_inds = \
                self.random_state_.choice(n, n // self.bs_ * self.bs_,
                                          replace=False).astype('int32')

            for batch_inds in np.array_split(epoch_inds, n // self.bs_):
                batch_x = self.centers_[batch_inds]
                kfeat = self._kernel(batch_x, self.centers_,
                                     Y_squared=self.centers_squared_)

                batch_y = Y[batch_inds]

                # Update 1: Sampled Coordinate Block.
                gradient = np.dot(kfeat, self.coef_) - batch_y
                self.coef_[batch_inds] = \
                    self.coef_[batch_inds] - step * gradient

                # Update 2: Fixed Coordinate Block
                delta = np.linalg.multi_dot([self.V_ * self.Q_,
                                             self.V_.T,
                                             kfeat[:, self.pinx_].T,
                                             gradient])
                self.coef_[self.pinx_] += step * delta
        return self

    def predict(self, X):
        """Predict using the kernel regression model

        Parameters
        ----------
        X : {float, array}, shape = [n_samples, n_features]
            Samples.

        Returns
        -------
        Y : {float, array}, shape = [n_samples, n_targets]
            Predicted targets.
        """
        check_is_fitted(self, ["bs_", "centers_", "centers_squared_", "coef_",
                               "eta_", "random_state_", "pinx_", "Q_", "V_", "was_1D_"])
        X = np.asarray(X, dtype=np.float32)
        if len(X.shape) == 1:
            raise ValueError("Reshape your data. X should be a matrix of shape"
                             " (n_samples, n_features).")
        n = X.shape[0]

        Ys = []
        for batch_inds in np.array_split(range(n), max(1, n // self.bs_)):
            batch_x = X[batch_inds]
            kfeat = self._kernel(batch_x, self.centers_,
                                 Y_squared=self.centers_squared_)

            pred = np.dot(kfeat, self.coef_)
            Ys.append(pred)

        Y = np.vstack(Ys)
        if self.was_1D_:
            Y = np.reshape(Y, Y.shape[0])

        return Y
