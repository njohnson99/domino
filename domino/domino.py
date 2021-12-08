from __future__ import annotations
import warnings
from dataclasses import dataclass
from functools import wraps
from typing import Union

import meerkat as mk
import numpy as np
import sklearn.cluster as cluster
from scipy import linalg
from scipy.special import logsumexp
from sklearn.decomposition import PCA
from sklearn.exceptions import ConvergenceWarning
from sklearn.mixture import GaussianMixture
from sklearn.mixture._base import _check_X, check_random_state
from sklearn.mixture._gaussian_mixture import (
    _compute_precision_cholesky,
    _estimate_gaussian_covariances_diag,
    _estimate_gaussian_covariances_full,
    _estimate_gaussian_covariances_spherical,
    _estimate_gaussian_covariances_tied,
)
from sklearn.preprocessing import label_binarize
from sklearn.utils.validation import check_is_fitted
from tqdm import tqdm

from .abstract import SliceDiscoveryMethod


class DominoSDM(SliceDiscoveryMethod):

    def __init__(
        self,
        n_slices: int = 5, 
        covariance_type: str = "diag",
        n_pca_components: Union[int, None] = 128,
        n_mixture_components: int = 25,
        init_params: str = "error",
        y_log_likelihood_weight: float = 1,
        y_hat_log_likelihood_weight: float = 1, 
        max_iter: int = 100,        
    ):
        """
        Slice Discovery based on the Domino Mixture Model. 

        A mixture model that jointly models the input embeddings, class labels, and 
        model predictions. This encourages slices that are homogeneous with respect to
        error type (e.g. all false positives). 
        
        The model assumes that data is generated 
        according to the following generative process: each example belongs to one slice 
        :math:`S` sampled from a categorical distribution 
        :math:`S \sim Cat(\mathbf{p}_S)` with parameter :math:`\mathbf{p}_S \in 
        \{\mathbf{p} \in \mathbb{R}_+^{\bar{k}} : \sum_{i = 1}^{\bar{k}} p_i = 1\}`. 
        Given the slice :math:`S'`, the embeddings are normally distributed 
        :math:`Z | S \sim \mathcal{N}(\mathbf{\mu}, \mathbf{\Sigma}`)  with parameters 
        mean :math:`\mathbf{\mu} \in \mathbb{R}^d`and :math:`\mathbf{\Sigma} \in 
        \mathbb{S}^{d}_{++}`(the set of symmetric positive definite :math:`d \times d`
        matrices), the labels vary as a categorical :math:`Y |S \sim Cat(\mathbf{p})`
        with parameter :math:`\mathbf{p} \in \{\mathbf{p} \in \mathbb{R}^c_+ : 
        \sum_{i = 1}^c p_i = 1\}`, and the model predictions also vary as a categorical 
        :math:`\hat{Y} | S \sim Cat(\mathbf{\hat{p}})`with parameter 
        :math:`\mathbf{\hat{p}} \in \{\mathbf{\hat{p}} \in \mathbb{R}^c_+ : 
        \sum_{i = 1}^c \hat{p}_i = 1\}`.
        Critically, this assumes that the embedding, label, and prediction are all 
        independent of one another conditioned on the slice. 
        
        The mixture model is, thus, parameterized by :math:`\phi = [\mathbf{p}_S, \mu, 
        \Sigma, \mathbf{p}, \mathbf{\hat{p}}]`. The log-likelihood over the 
        :math:`n`examples in the validation dataset :math:`D_v`is given as follows 
        and maximized using expectation-maximization: 
        :math:`\ell(\phi) = \sum_{i=1}^n \log \sum_{s=1}^{\hat{k}} P(S=s)P(Z=z_i| S=s)
        P( Y=y_i| S=s)P(\hat{Y} = h_\theta(x_i) | S=s)`.

        We include an optional hyperparameter :math:`\gamma \in \mathbb{R}_+` that 
        balances the importance of modeling the class labels and predictions against the
        importance of modeling the embedding. The modified log-likelihood ove :math:`n` 
        examples is given as follows and maximized using expectation-maximization: 
        :math:`\ell(\phi) = \sum_{i=1}^n \log \sum_{s=1}^{\hat{k}} P(S=s)P(Z=z_i| S=s)
        P( Y=y_i| S=s)^\gamma P(\hat{Y} = h_\theta(x_i) | S=s)^\gamma`. 
        Args:
            n_slices (int, optional): The number of slices to discover. Defaults to 5.
            covariance_type (str, optional): The type of covariance matrix to use. Same 
                as in sklearn.mixture.GaussianMixture. Defaults to "diag", which is 
                recommended.
            n_pca_components (Union[int, None], optional): The number of PCA components 
                to use. If ``None``, then no PCA is performed. Defaults to 128.
            n_mixture_components (int, optional): The number of clusters in the mixture
                model. This differs from ``n_slices`` in that ``DominoSDM`` only 
                returns the top ``n_slices`` with the highest error rate of the 
                ``n_mixture_components``. Defaults to 25.
            init_params (str, optional): The initialization method to use. Options are 
                the same as in sklearn.mixture.GaussianMixture plus one addition, 
                "error". If "error", then TODO
                Defaults to "error".
            y_log_likelihood_weight (float, optional): The weight of the y term in 
                in the log-likelihood.
            y_hat_log_likelihood_weight (float, optional): The weight of the y_hat term
                in the log-likelihood.
            max_iter (int, optional): The maximum number of iterations to run. Defaults
                to 100.
        """
        super().__init__(n_slices=n_slices)

        self.config.covariance_type = covariance_type
        self.config.n_pca_components = n_pca_components
        self.config.n_mixture_components = n_mixture_components
        self.config.init_params = init_params
        self.config.y_log_likelihood_weight = y_log_likelihood_weight
        self.config.y_hat_log_likelihood_weight = y_hat_log_likelihood_weight
        self.config.max_iter = max_iter
        
        if self.config.n_pca_components is None:
            self.pca = None
        else:
            self.pca = PCA(n_components=self.config.n_pca_components)

        self.gmm = DominoMixture(
            n_components=self.config.n_mixture_components,
            weight_y_log_likelihood=self.config.weight_y_log_likelihood,
            covariance_type=self.config.covariance_type,
            init_params=self.config.init_params,
            max_iter=self.config.max_iter,
        )

    def fit(
        self,
        data: Union[dict, mk.DataPanel] = None,
        embeddings: Union[str, np.ndarray] = "embedding",
        targets: Union[str, np.ndarray] = "target",
        pred_probs: Union[str, np.ndarray] = "pred_probs",
    ) -> DominoSDM:
        """
        Fit the DominoSDM to the data.

        Args:
            data (mk.DataPanel, optional): Input Meerkat DataPanel with a NumPyfor
                embeddings, targets, pred_probs,
                as described below. Defaults to None.
            embeddings (Union[str, np.ndarray], optional): The name of the embedding
                column in ``data`` or, if ``data`` is ``None``, then embeddings as an
                NumPy array of shape (num_examples, embedding_dimension). Defaults to
                "embedding".
            targets (Union[str, np.ndarray], optional): The name of the target column in
                ``data`` or, if ``data`` is ``None``, then the targets as an NumPy array
                of shape (num_examples,). Defaults to "target".
            pred_probs (Union[str, np.ndarray], optional): The name of the
                predicted probability column in ``data`` or, if ``data`` is ``None``,
                then the predicted probabilities as an NumPy array of shape
                (num_examples, num_classes). Defaults to "pred_probs".

        Returns:
            DominoSDM: Returns the fit instance of DominoSDM.
        """
        if (
            any(map(lambda x: isinstance(x, str), [embeddings, targets, pred_probs]))
            and data is None
        ):
            raise ValueError(
                "If `embeddings`, `target` or `pred_probs` are strings, `data`"
                " must be provided."
            )
        embeddings = data[embeddings] if isinstance(embeddings, str) else embeddings
        targets = data[targets] if isinstance(targets, str) else targets
        pred_probs = data[pred_probs] if isinstance(pred_probs, str) else pred_probs

        if self.pca is not None:
            self.pca.fit(X=embeddings)
            embeddings = self.pca.transform(X=embeddings)

        self.gmm.fit(X=embeddings, y=targets, y_hat=pred_probs)

        self.slice_cluster_indices = (
            -np.abs((self.gmm.y_probs[:, 1] - self.gmm.y_hat_probs[:, 1]))
        ).argsort()[: self.config.n_slices]
        return self

    def transform(
        self,
        data: Union[dict, mk.DataPanel] = None,
        embeddings: Union[str, np.ndarray] = "embedding",
        targets: Union[str, np.ndarray] = "target",
        pred_probs: Union[str, np.ndarray] = "pred_probs",
    ) -> np.ndarray:
        """
        Fit the DominoSDM to the data.

        Args:
            data (mk.DataPanel, optional): Input Meerkat DataPanel with a NumPy for
                embeddings, targets, pred_probs,
                as described below. Defaults to None.
            embeddings (Union[str, np.ndarray], optional): The name of the embedding
                column in ``data`` or, if ``data`` is ``None``, then embeddings as an
                NumPy array of shape (num_examples, embedding_dimension). Defaults to
                "embedding".
            targets (Union[str, np.ndarray], optional): The name of the target column in
                ``data`` or, if ``data`` is ``None``, then the targets as an NumPy array
                of shape (num_examples,). Defaults to "target".
            pred_probs (Union[str, np.ndarray], optional): The name of the
                predicted probability column in ``data`` or, if ``data`` is ``None``,
                then the predicted probabilities as an NumPy array of shape
                (num_examples, num_classes). Defaults to "pred_probs".

        Returns:
            np.ndarray: A ``np.ndarray`` of shape (num_examples, num_slices).
        """
        if (
            any(map(lambda x: isinstance(x, str), [embeddings, targets, pred_probs]))
            and data is None
        ):
            raise ValueError(
                "If `embeddings`, `target` or `pred_probs` are strings, `data`"
                " must be provided."
            )
        embeddings = data[embeddings] if isinstance(embeddings, str) else embeddings
        targets = data[targets] if isinstance(targets, str) else targets
        pred_probs = data[pred_probs] if isinstance(pred_probs, str) else pred_probs

        if self.pca is not None:
            embeddings = self.pca.transform(X=embeddings)

        clusters = self.gmm.predict_proba(embeddings, y=targets, y_hat=pred_probs)

        return clusters[:, self.slice_cluster_indices]


class DominoMixture(GaussianMixture):
    @wraps(GaussianMixture.__init__)
    def __init__(self, *args, weight_y_log_likelihood: float = 1, **kwargs):
        self.weight_y_log_likelihood = weight_y_log_likelihood
        super().__init__(*args, **kwargs)

    def _initialize_parameters(self, X, y, y_hat, random_state):
        """Initialize the model parameters.

        Parameters
        ----------
        X : array-like of shape  (n_samples, n_features)

        random_state : RandomState
            A random number generator instance that controls the random seed
            used for the method chosen to initialize the parameters.
        """
        n_samples, _ = X.shape

        if self.init_params == "kmeans":
            resp = np.zeros((n_samples, self.n_components))
            label = (
                cluster.KMeans(
                    n_clusters=self.n_components, n_init=1, random_state=random_state
                )
                .fit(X)
                .labels_
            )
            resp[np.arange(n_samples), label] = 1
        elif self.init_params == "random":
            resp = random_state.rand(n_samples, self.n_components)
            resp /= resp.sum(axis=1)[:, np.newaxis]
        elif self.init_params == "error":
            num_classes = y.shape[-1]
            if self.n_components < num_classes ** 2:
                raise ValueError(
                    "Can't use parameter init 'error' when "
                    "`n_components` < `num_classes **2`"
                )
            resp = np.matmul(y[:, :, np.newaxis], y_hat[:, np.newaxis, :]).reshape(
                len(y), -1
            )
            resp = np.concatenate(
                [resp]
                * (
                    int(self.n_components / (num_classes ** 2))
                    + (self.n_components % (num_classes ** 2) > 0)
                ),
                axis=1,
            )[:, : self.n_components]
            resp /= resp.sum(axis=1)[:, np.newaxis]

            resp += random_state.rand(n_samples, self.n_components)
            resp /= resp.sum(axis=1)[:, np.newaxis]

        else:
            raise ValueError(
                "Unimplemented initialization method '%s'" % self.init_params
            )

        self._initialize(X, y, y_hat, resp)

    def _initialize(self, X, y, y_hat, resp):
        """Initialization of the Gaussian mixture parameters.
        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
        resp : array-like of shape (n_samples, n_components)
        """
        n_samples, _ = X.shape

        weights, means, covariances, y_probs, y_hat_probs = _estimate_parameters(
            X, y, y_hat, resp, self.reg_covar, self.covariance_type
        )
        weights /= n_samples

        self.weights_ = weights if self.weights_init is None else self.weights_init
        self.means_ = means if self.means_init is None else self.means_init
        self.y_probs, self.y_hat_probs = y_probs, y_hat_probs
        if self.precisions_init is None:
            self.covariances_ = covariances
            self.precisions_cholesky_ = _compute_precision_cholesky(
                covariances, self.covariance_type
            )
        elif self.covariance_type == "full":
            self.precisions_cholesky_ = np.array(
                [
                    linalg.cholesky(prec_init, lower=True)
                    for prec_init in self.precisions_init
                ]
            )
        elif self.covariance_type == "tied":
            self.precisions_cholesky_ = linalg.cholesky(
                self.precisions_init, lower=True
            )
        else:
            self.precisions_cholesky_ = self.precisions_init

    def fit(self, X, y, y_hat):

        self.fit_predict(X, y, y_hat)
        return self

    def _preprocess_ys(self, y: np.ndarray = None, y_hat: np.ndarray = None):
        if y is not None:
            y = label_binarize(y, classes=np.arange(np.max(y) + 1))
            if y.shape[-1] == 1:
                # binary targets transform to a column vector with label_binarize
                y = np.array([1 - y[:, 0], y[:, 0]]).T
        if y is not None:
            if len(y_hat.shape) == 1:
                y_hat = np.array([1 - y_hat, y_hat]).T
        return y, y_hat

    def fit_predict(self, X, y, y_hat):
        y, y_hat = self._preprocess_ys(y, y_hat)

        X = _check_X(X, self.n_components, ensure_min_samples=2)
        self._check_n_features(X, reset=True)
        self._check_initial_parameters(X)

        # if we enable warm_start, we will have a unique initialisation
        do_init = not (self.warm_start and hasattr(self, "converged_"))
        n_init = self.n_init if do_init else 1

        max_lower_bound = -np.infty
        self.converged_ = False

        random_state = check_random_state(self.random_state)

        n_samples, _ = X.shape
        for init in range(n_init):
            self._print_verbose_msg_init_beg(init)

            if do_init:
                self._initialize_parameters(X, y, y_hat, random_state)

            lower_bound = -np.infty if do_init else self.lower_bound_

            for n_iter in tqdm(range(1, self.max_iter + 1)):  # removed tqdm from here
                prev_lower_bound = lower_bound

                log_prob_norm, log_resp = self._e_step(X, y, y_hat)
                self._m_step(X, y, y_hat, log_resp)
                lower_bound = self._compute_lower_bound(log_resp, log_prob_norm)
                change = lower_bound - prev_lower_bound
                self._print_verbose_msg_iter_end(n_iter, change)

                if abs(change) < self.tol:
                    self.converged_ = True
                    break

            self._print_verbose_msg_init_end(lower_bound)

            if lower_bound > max_lower_bound:
                max_lower_bound = lower_bound
                best_params = self._get_parameters()
                best_n_iter = n_iter

        if not self.converged_:
            warnings.warn(
                "Initialization %d did not converge. "
                "Try different init parameters, "
                "or increase max_iter, tol "
                "or check for degenerate data." % (init + 1),
                ConvergenceWarning,
            )

        self._set_parameters(best_params)
        self.n_iter_ = best_n_iter
        self.lower_bound_ = max_lower_bound

        # Always do a final e-step to guarantee that the labels returned by
        # fit_predict(X) are always consistent with fit(X).predict(X)
        # for any value of max_iter and tol (and any random_state).
        _, log_resp = self._e_step(X, y, y_hat)

        return log_resp.argmax(axis=1)

    def predict_proba(
        self, X: np.ndarray, y: np.ndarray = None, y_hat: np.ndarray = None
    ):
        y, y_hat = self._preprocess_ys(y, y_hat)

        check_is_fitted(self)
        X = _check_X(X, None, self.means_.shape[1])
        _, log_resp = self._estimate_log_prob_resp(X, y, y_hat)
        return np.exp(log_resp)

    def _m_step(self, X, y, y_hat, log_resp):
        """M step.
        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
        log_resp : array-like of shape (n_samples, n_components)
            Logarithm of the posterior probabilities (or responsibilities) of
            the point of each sample in X.
        """
        resp = np.exp(log_resp)
        n_samples, _ = X.shape
        (
            self.weights_,
            self.means_,
            self.covariances_,
            self.y_probs,
            self.y_hat_probs,
        ) = _estimate_parameters(
            X, y, y_hat, resp, self.reg_covar, self.covariance_type
        )
        self.weights_ /= n_samples
        self.precisions_cholesky_ = _compute_precision_cholesky(
            self.covariances_, self.covariance_type
        )

    def _e_step(self, X, y, y_hat):
        """E step.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)

        Returns
        -------
        log_prob_norm : float
            Mean of the logarithms of the probabilities of each sample in X

        log_responsibility : array, shape (n_samples, n_components)
            Logarithm of the posterior probabilities (or responsibilities) of
            the point of each sample in X.
        """
        log_prob_norm, log_resp = self._estimate_log_prob_resp(X, y, y_hat)
        return np.mean(log_prob_norm), log_resp

    def _estimate_log_prob_resp(self, X, y=None, y_hat=None):
        """Estimate log probabilities and responsibilities for each sample.

        Compute the log probabilities, weighted log probabilities per
        component and responsibilities for each sample in X with respect to
        the current state of the model.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)

        Returns
        -------
        log_prob_norm : array, shape (n_samples,)
            log p(X)

        log_responsibilities : array, shape (n_samples, n_components)
            logarithm of the responsibilities
        """
        weighted_log_prob = self._estimate_weighted_log_prob(X, y, y_hat)
        log_prob_norm = logsumexp(weighted_log_prob, axis=1)
        with np.errstate(under="ignore"):
            # ignore underflow
            log_resp = weighted_log_prob - log_prob_norm[:, np.newaxis]
        return log_prob_norm, log_resp

    def _estimate_weighted_log_prob(self, X, y=None, y_hat=None):
        log_prob = self._estimate_log_prob(X) + self._estimate_log_weights()

        if y is not None:
            log_prob += self._estimate_y_log_prob(y) * self.weight_y_log_likelihood

        if y_hat is not None:
            log_prob += (
                self._estimate_y_hat_log_prob(y_hat) * self.weight_y_log_likelihood
            )

        return log_prob

    def _get_parameters(self):
        return (
            self.weights_,
            self.means_,
            self.covariances_,
            self.y_probs,
            self.y_hat_probs,
            self.precisions_cholesky_,
        )

    def _set_parameters(self, params):
        (
            self.weights_,
            self.means_,
            self.covariances_,
            self.y_probs,
            self.y_hat_probs,
            self.precisions_cholesky_,
        ) = params

        # Attributes computation
        _, n_features = self.means_.shape

        if self.covariance_type == "full":
            self.precisions_ = np.empty(self.precisions_cholesky_.shape)
            for k, prec_chol in enumerate(self.precisions_cholesky_):
                self.precisions_[k] = np.dot(prec_chol, prec_chol.T)

        elif self.covariance_type == "tied":
            self.precisions_ = np.dot(
                self.precisions_cholesky_, self.precisions_cholesky_.T
            )
        else:
            self.precisions_ = self.precisions_cholesky_ ** 2

    def _n_parameters(self):
        """Return the number of free parameters in the model."""
        return super()._n_parameters() + 2 * self.n_components

    def _estimate_y_log_prob(self, y):
        """Estimate the Gaussian distribution parameters.

        Parameters
        ----------
        y: array-like of shape (n_samples, n_classes)

        y_hat: array-like of shpae (n_samples, n_classes)
        """
        # add epsilon to avoid "RuntimeWarning: divide by zero encountered in log"
        return np.log(np.dot(y, self.y_probs.T) + np.finfo(self.y_probs.dtype).eps)

    def _estimate_y_hat_log_prob(self, y_hat):
        """Estimate the Gaussian distribution parameters.

        Parameters
        ----------
        y: array-like of shape (n_samples, n_classes)

        y_hat: array-like of shpae (n_samples, n_classes)
        """
        # add epsilon to avoid "RuntimeWarning: divide by zero encountered in log"
        return np.log(
            np.dot(y_hat, self.y_hat_probs.T) + np.finfo(self.y_hat_probs.dtype).eps
        )


def _estimate_parameters(X, y, y_hat, resp, reg_covar, covariance_type):
    """Estimate the Gaussian distribution parameters.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        The input data array.

    y: array-like of shape (n_samples, n_classes)

    y_hat: array-like of shpae (n_samples, n_classes)

    resp : array-like of shape (n_samples, n_components)
        The responsibilities for each data sample in X.

    reg_covar : float
        The regularization added to the diagonal of the covariance matrices.

    covariance_type : {'full', 'tied', 'diag', 'spherical'}
        The type of precision matrices.

    Returns
    -------
    nk : array-like of shape (n_components,)
        The numbers of data samples in the current components.

    means : array-like of shape (n_components, n_features)
        The centers of the current components.

    covariances : array-like
        The covariance matrix of the current components.
        The shape depends of the covariance_type.
    """
    nk = resp.sum(axis=0) + 10 * np.finfo(resp.dtype).eps  # (n_components, )
    means = np.dot(resp.T, X) / nk[:, np.newaxis]
    covariances = {
        "full": _estimate_gaussian_covariances_full,
        "tied": _estimate_gaussian_covariances_tied,
        "diag": _estimate_gaussian_covariances_diag,
        "spherical": _estimate_gaussian_covariances_spherical,
    }[covariance_type](resp, X, nk, means, reg_covar)

    y_probs = np.dot(resp.T, y) / nk[:, np.newaxis]  # (n_components, n_classes)
    y_hat_probs = np.dot(resp.T, y_hat) / nk[:, np.newaxis]  # (n_components, n_classes)

    return nk, means, covariances, y_probs, y_hat_probs
