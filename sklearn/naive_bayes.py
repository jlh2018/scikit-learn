# -*- coding: utf-8 -*-

"""
The :mod:`sklearn.naive_bayes` module implements Naive Bayes algorithms. These
are supervised learning methods based on applying Bayes' theorem with strong
(naive) feature independence assumptions.
"""

# Author: Vincent Michel <vincent.michel@inria.fr>
#         Minor fixes by Fabian Pedregosa
#         Amit Aides <amitibo@tx.technion.ac.il>
#         Yehuda Finkelstein <yehudaf@tx.technion.ac.il>
#         Lars Buitinck
#         Jan Hendrik Metzen <jhm@informatik.uni-bremen.de>
#         (parts based on earlier work by Mathieu Blondel)
#
# License: BSD 3 clause
import warnings

from abc import ABCMeta, abstractmethod


import numpy as np
from scipy.sparse import issparse

from .base import BaseEstimator, ClassifierMixin
from .preprocessing import binarize
from .preprocessing import LabelBinarizer
from .preprocessing import label_binarize
from .utils import check_X_y, check_array, check_consistent_length
from .utils.extmath import safe_sparse_dot
from .utils.fixes import logsumexp
from .utils.multiclass import _check_partial_fit_first_call
from .utils.validation import check_is_fitted, check_non_negative

__all__ = ['BernoulliNB', 'GaussianNB', 'MultinomialNB', 'ComplementNB',
           'CategoricalNB']


class BaseNB(ClassifierMixin, BaseEstimator, metaclass=ABCMeta):
    """Abstract base class for naive Bayes estimators"""

    @abstractmethod
    def _joint_log_likelihood(self, X):
        """Compute the unnormalized posterior log probability of X

        I.e. ``log P(c) + log P(x|c)`` for all rows x of X, as an array-like of
        shape [n_classes, n_samples].

        Input is passed to _joint_log_likelihood as-is by predict,
        predict_proba and predict_log_proba.
        """

    @abstractmethod
    def _check_X(self, X):
        """Validate input X
        """
        pass

    def predict(self, X):
        """
        Perform classification on an array of test vectors X.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)

        Returns
        -------
        C : ndarray of shape (n_samples,)
            Predicted target values for X
        """
        check_is_fitted(self)
        X = self._check_X(X)
        jll = self._joint_log_likelihood(X)
        return self.classes_[np.argmax(jll, axis=1)]

    def predict_log_proba(self, X):
        """
        Return log-probability estimates for the test vector X.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)

        Returns
        -------
        C : array-like of shape (n_samples, n_classes)
            Returns the log-probability of the samples for each class in
            the model. The columns correspond to the classes in sorted
            order, as they appear in the attribute :term:`classes_`.
        """
        check_is_fitted(self)
        X = self._check_X(X)
        jll = self._joint_log_likelihood(X)
        # normalize by P(x) = P(f_1, ..., f_n)
        log_prob_x = logsumexp(jll, axis=1)
        return jll - np.atleast_2d(log_prob_x).T

    def predict_proba(self, X):
        """
        Return probability estimates for the test vector X.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)

        Returns
        -------
        C : array-like of shape (n_samples, n_classes)
            Returns the probability of the samples for each class in
            the model. The columns correspond to the classes in sorted
            order, as they appear in the attribute :term:`classes_`.
        """
        return np.exp(self.predict_log_proba(X))


class GaussianNB(BaseNB):
    """
    Gaussian Naive Bayes (GaussianNB)

    Can perform online updates to model parameters via :meth:`partial_fit`.
    For details on algorithm used to update feature means and variance online,
    see Stanford CS tech report STAN-CS-79-773 by Chan, Golub, and LeVeque:

        http://i.stanford.edu/pub/cstr/reports/cs/tr/79/773/CS-TR-79-773.pdf

    Read more in the :ref:`User Guide <gaussian_naive_bayes>`.

    Parameters
    ----------
    priors : array-like, shape (n_classes,)
        Prior probabilities of the classes. If specified the priors are not
        adjusted according to the data.

    var_smoothing : float, optional (default=1e-9)
        Portion of the largest variance of all features that is added to
        variances for calculation stability.

    Attributes
    ----------
    class_prior_ : array, shape (n_classes,)
        probability of each class.

    class_count_ : array, shape (n_classes,)
        number of training samples observed in each class.

    classes_ : array, shape (n_classes,)
        class labels known to the classifier

    theta_ : array, shape (n_classes, n_features)
        mean of each feature per class

    sigma_ : array, shape (n_classes, n_features)
        variance of each feature per class

    epsilon_ : float
        absolute additive value to variances

    classes_ : array-like, shape (n_classes,)
        Unique class labels.

    Examples
    --------
    >>> import numpy as np
    >>> X = np.array([[-1, -1], [-2, -1], [-3, -2], [1, 1], [2, 1], [3, 2]])
    >>> Y = np.array([1, 1, 1, 2, 2, 2])
    >>> from sklearn.naive_bayes import GaussianNB
    >>> clf = GaussianNB()
    >>> clf.fit(X, Y)
    GaussianNB()
    >>> print(clf.predict([[-0.8, -1]]))
    [1]
    >>> clf_pf = GaussianNB()
    >>> clf_pf.partial_fit(X, Y, np.unique(Y))
    GaussianNB()
    >>> print(clf_pf.predict([[-0.8, -1]]))
    [1]
    """

    def __init__(self, priors=None, var_smoothing=1e-9):
        self.priors = priors
        self.var_smoothing = var_smoothing

    def fit(self, X, y, sample_weight=None):
        """Fit Gaussian Naive Bayes according to X, y

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples
            and n_features is the number of features.

        y : array-like, shape (n_samples,)
            Target values.

        sample_weight : array-like, shape (n_samples,), optional (default=None)
            Weights applied to individual samples (1. for unweighted).

            .. versionadded:: 0.17
               Gaussian Naive Bayes supports fitting with *sample_weight*.

        Returns
        -------
        self : object
        """
        return self._partial_fit(X, y, np.unique(y), _refit=True,
                                 sample_weight=sample_weight)

    def _check_X(self, X):
        return check_array(X)

    @staticmethod
    def _update_mean_variance(n_past, mu, var, X, sample_weight=None):
        """Compute online update of Gaussian mean and variance.

        Given starting sample count, mean, and variance, a new set of
        points X, and optionally sample weights, return the updated mean and
        variance. (NB - each dimension (column) in X is treated as independent
        -- you get variance, not covariance).

        Can take scalar mean and variance, or vector mean and variance to
        simultaneously update a number of independent Gaussians.

        See Stanford CS tech report STAN-CS-79-773 by Chan, Golub, and LeVeque:

        http://i.stanford.edu/pub/cstr/reports/cs/tr/79/773/CS-TR-79-773.pdf

        Parameters
        ----------
        n_past : int
            Number of samples represented in old mean and variance. If sample
            weights were given, this should contain the sum of sample
            weights represented in old mean and variance.

        mu : array-like, shape (number of Gaussians,)
            Means for Gaussians in original set.

        var : array-like, shape (number of Gaussians,)
            Variances for Gaussians in original set.

        sample_weight : array-like, shape (n_samples,), optional (default=None)
            Weights applied to individual samples (1. for unweighted).

        Returns
        -------
        total_mu : array-like, shape (number of Gaussians,)
            Updated mean for each Gaussian over the combined set.

        total_var : array-like, shape (number of Gaussians,)
            Updated variance for each Gaussian over the combined set.
        """
        if X.shape[0] == 0:
            return mu, var

        # Compute (potentially weighted) mean and variance of new datapoints
        if sample_weight is not None:
            n_new = float(sample_weight.sum())
            new_mu = np.average(X, axis=0, weights=sample_weight)
            new_var = np.average((X - new_mu) ** 2, axis=0,
                                 weights=sample_weight)
        else:
            n_new = X.shape[0]
            new_var = np.var(X, axis=0)
            new_mu = np.mean(X, axis=0)

        if n_past == 0:
            return new_mu, new_var

        n_total = float(n_past + n_new)

        # Combine mean of old and new data, taking into consideration
        # (weighted) number of observations
        total_mu = (n_new * new_mu + n_past * mu) / n_total

        # Combine variance of old and new data, taking into consideration
        # (weighted) number of observations. This is achieved by combining
        # the sum-of-squared-differences (ssd)
        old_ssd = n_past * var
        new_ssd = n_new * new_var
        total_ssd = (old_ssd + new_ssd +
                     (n_new * n_past / n_total) * (mu - new_mu) ** 2)
        total_var = total_ssd / n_total

        return total_mu, total_var

    def partial_fit(self, X, y, classes=None, sample_weight=None):
        """Incremental fit on a batch of samples.

        This method is expected to be called several times consecutively
        on different chunks of a dataset so as to implement out-of-core
        or online learning.

        This is especially useful when the whole dataset is too big to fit in
        memory at once.

        This method has some performance and numerical stability overhead,
        hence it is better to call partial_fit on chunks of data that are
        as large as possible (as long as fitting in the memory budget) to
        hide the overhead.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        y : array-like, shape (n_samples,)
            Target values.

        classes : array-like, shape (n_classes,), optional (default=None)
            List of all the classes that can possibly appear in the y vector.

            Must be provided at the first call to partial_fit, can be omitted
            in subsequent calls.

        sample_weight : array-like, shape (n_samples,), optional (default=None)
            Weights applied to individual samples (1. for unweighted).

            .. versionadded:: 0.17

        Returns
        -------
        self : object
        """
        return self._partial_fit(X, y, classes, _refit=False,
                                 sample_weight=sample_weight)

    def _partial_fit(self, X, y, classes=None, _refit=False,
                     sample_weight=None):
        """Actual implementation of Gaussian NB fitting.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        y : array-like, shape (n_samples,)
            Target values.

        classes : array-like, shape (n_classes,), optional (default=None)
            List of all the classes that can possibly appear in the y vector.

            Must be provided at the first call to partial_fit, can be omitted
            in subsequent calls.

        _refit : bool, optional (default=False)
            If true, act as though this were the first time we called
            _partial_fit (ie, throw away any past fitting and start over).

        sample_weight : array-like, shape (n_samples,), optional (default=None)
            Weights applied to individual samples (1. for unweighted).

        Returns
        -------
        self : object
        """
        X, y = check_X_y(X, y)
        if sample_weight is not None:
            sample_weight = check_array(sample_weight, ensure_2d=False)
            check_consistent_length(y, sample_weight)

        # If the ratio of data variance between dimensions is too small, it
        # will cause numerical errors. To address this, we artificially
        # boost the variance by epsilon, a small fraction of the standard
        # deviation of the largest dimension.
        self.epsilon_ = self.var_smoothing * np.var(X, axis=0).max()

        if _refit:
            self.classes_ = None

        if _check_partial_fit_first_call(self, classes):
            # This is the first call to partial_fit:
            # initialize various cumulative counters
            n_features = X.shape[1]
            n_classes = len(self.classes_)
            self.theta_ = np.zeros((n_classes, n_features))
            self.sigma_ = np.zeros((n_classes, n_features))

            self.class_count_ = np.zeros(n_classes, dtype=np.float64)

            # Initialise the class prior
            # Take into account the priors
            if self.priors is not None:
                priors = np.asarray(self.priors)
                # Check that the provide prior match the number of classes
                if len(priors) != n_classes:
                    raise ValueError('Number of priors must match number of'
                                     ' classes.')
                # Check that the sum is 1
                if not np.isclose(priors.sum(), 1.0):
                    raise ValueError('The sum of the priors should be 1.')
                # Check that the prior are non-negative
                if (priors < 0).any():
                    raise ValueError('Priors must be non-negative.')
                self.class_prior_ = priors
            else:
                # Initialize the priors to zeros for each class
                self.class_prior_ = np.zeros(len(self.classes_),
                                             dtype=np.float64)
        else:
            if X.shape[1] != self.theta_.shape[1]:
                msg = "Number of features %d does not match previous data %d."
                raise ValueError(msg % (X.shape[1], self.theta_.shape[1]))
            # Put epsilon back in each time
            self.sigma_[:, :] -= self.epsilon_

        classes = self.classes_

        unique_y = np.unique(y)
        unique_y_in_classes = np.in1d(unique_y, classes)

        if not np.all(unique_y_in_classes):
            raise ValueError("The target label(s) %s in y do not exist in the "
                             "initial classes %s" %
                             (unique_y[~unique_y_in_classes], classes))

        for y_i in unique_y:
            i = classes.searchsorted(y_i)
            X_i = X[y == y_i, :]

            if sample_weight is not None:
                sw_i = sample_weight[y == y_i]
                N_i = sw_i.sum()
            else:
                sw_i = None
                N_i = X_i.shape[0]

            new_theta, new_sigma = self._update_mean_variance(
                self.class_count_[i], self.theta_[i, :], self.sigma_[i, :],
                X_i, sw_i)

            self.theta_[i, :] = new_theta
            self.sigma_[i, :] = new_sigma
            self.class_count_[i] += N_i

        self.sigma_[:, :] += self.epsilon_

        # Update if only no priors is provided
        if self.priors is None:
            # Empirical prior, with sample_weight taken into account
            self.class_prior_ = self.class_count_ / self.class_count_.sum()

        return self

    def _joint_log_likelihood(self, X):
        joint_log_likelihood = []
        for i in range(np.size(self.classes_)):
            jointi = np.log(self.class_prior_[i])
            n_ij = - 0.5 * len(X) * (np.log(2. * np.pi * self.sigma_[i, :]))
            n_ij -= 0.5 * np.sum(((X - self.theta_[i, :]) ** 2) /
                                 (self.sigma_[i, :]), 1)
            joint_log_likelihood.append(jointi + n_ij)

        joint_log_likelihood = np.array(joint_log_likelihood).T
        return joint_log_likelihood


_ALPHA_MIN = 1e-10


class BaseDiscreteNB(BaseNB):
    """Abstract base class for naive Bayes on discrete/categorical data

    Any estimator based on this class should provide:

    __init__
    _joint_log_likelihood(X) as per BaseNB
    """

    def _check_X(self, X):
        return check_array(X, accept_sparse='csr')

    def _check_X_y(self, X, y):
        return check_X_y(X, y, accept_sparse='csr')

    def _update_class_log_prior(self, class_prior=None):
        n_classes = len(self.classes_)
        if class_prior is not None:
            if len(class_prior) != n_classes:
                raise ValueError("Number of priors must match number of"
                                 " classes.")
            self.class_log_prior_ = np.log(class_prior)
        elif self.fit_prior:
            with warnings.catch_warnings():
                # silence the warning when count is 0 because class was not yet
                # observed
                warnings.simplefilter("ignore", RuntimeWarning)
                log_class_count = np.log(self.class_count_)

            # empirical prior, with sample_weight taken into account
            self.class_log_prior_ = (log_class_count -
                                     np.log(self.class_count_.sum()))
        else:
            self.class_log_prior_ = np.full(n_classes, -np.log(n_classes))

    def _check_alpha(self):
        if np.min(self.alpha) < 0:
            raise ValueError('Smoothing parameter alpha = %.1e. '
                             'alpha should be > 0.' % np.min(self.alpha))
        if isinstance(self.alpha, np.ndarray):
            if not self.alpha.shape[0] == self.n_features_:
                raise ValueError("alpha should be a scalar or a numpy array "
                                 "with shape [n_features]")
        if np.min(self.alpha) < _ALPHA_MIN:
            warnings.warn('alpha too small will result in numeric errors, '
                          'setting alpha = %.1e' % _ALPHA_MIN)
            return np.maximum(self.alpha, _ALPHA_MIN)
        return self.alpha

    def partial_fit(self, X, y, classes=None, sample_weight=None):
        """Incremental fit on a batch of samples.

        This method is expected to be called several times consecutively
        on different chunks of a dataset so as to implement out-of-core
        or online learning.

        This is especially useful when the whole dataset is too big to fit in
        memory at once.

        This method has some performance overhead hence it is better to call
        partial_fit on chunks of data that are as large as possible
        (as long as fitting in the memory budget) to hide the overhead.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        y : array-like of shape (n_samples,)
            Target values.

        classes : array-like of shape (n_classes) (default=None)
            List of all the classes that can possibly appear in the y vector.

            Must be provided at the first call to partial_fit, can be omitted
            in subsequent calls.

        sample_weight : array-like of shape (n_samples,), default=None
            Weights applied to individual samples (1. for unweighted).

        Returns
        -------
        self : object
        """
        X, y = self._check_X_y(X, y)
        _, n_features = X.shape

        if _check_partial_fit_first_call(self, classes):
            # This is the first call to partial_fit:
            # initialize various cumulative counters
            n_effective_classes = len(classes) if len(classes) > 1 else 2
            self._init_counters(n_effective_classes, n_features)
            self.n_features_ = n_features
        elif n_features != self.n_features_:
            msg = "Number of features %d does not match previous data %d."
            raise ValueError(msg % (n_features, self.n_features_))

        Y = label_binarize(y, classes=self.classes_)
        if Y.shape[1] == 1:
            Y = np.concatenate((1 - Y, Y), axis=1)

        if X.shape[0] != Y.shape[0]:
            msg = "X.shape[0]=%d and y.shape[0]=%d are incompatible."
            raise ValueError(msg % (X.shape[0], y.shape[0]))

        # label_binarize() returns arrays with dtype=np.int64.
        # We convert it to np.float64 to support sample_weight consistently
        Y = Y.astype(np.float64, copy=False)
        if sample_weight is not None:
            sample_weight = np.atleast_2d(sample_weight)
            Y *= check_array(sample_weight).T

        class_prior = self.class_prior

        # Count raw events from data before updating the class log prior
        # and feature log probas
        self._count(X, Y)

        # XXX: OPTIM: we could introduce a public finalization method to
        # be called by the user explicitly just once after several consecutive
        # calls to partial_fit and prior any call to predict[_[log_]proba]
        # to avoid computing the smooth log probas at each call to partial fit
        alpha = self._check_alpha()
        self._update_feature_log_prob(alpha)
        self._update_class_log_prior(class_prior=class_prior)
        return self

    def fit(self, X, y, sample_weight=None):
        """Fit Naive Bayes classifier according to X, y

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        y : array-like of shape (n_samples,)
            Target values.

        sample_weight : array-like of shape (n_samples,), default=None
            Weights applied to individual samples (1. for unweighted).

        Returns
        -------
        self : object
        """
        X, y = self._check_X_y(X, y)
        _, n_features = X.shape
        self.n_features_ = n_features

        labelbin = LabelBinarizer()
        Y = labelbin.fit_transform(y)
        self.classes_ = labelbin.classes_
        if Y.shape[1] == 1:
            Y = np.concatenate((1 - Y, Y), axis=1)

        # LabelBinarizer().fit_transform() returns arrays with dtype=np.int64.
        # We convert it to np.float64 to support sample_weight consistently;
        # this means we also don't have to cast X to floating point
        if sample_weight is not None:
            Y = Y.astype(np.float64, copy=False)
            sample_weight = np.atleast_2d(sample_weight)
            Y *= check_array(sample_weight).T

        class_prior = self.class_prior

        # Count raw events from data before updating the class log prior
        # and feature log probas
        n_effective_classes = Y.shape[1]

        self._init_counters(n_effective_classes, n_features)
        self._count(X, Y)
        alpha = self._check_alpha()
        self._update_feature_log_prob(alpha)
        self._update_class_log_prior(class_prior=class_prior)
        return self

    def _init_counters(self, n_effective_classes, n_features):
        self.class_count_ = np.zeros(n_effective_classes, dtype=np.float64)
        self.feature_count_ = np.zeros((n_effective_classes, n_features),
                                       dtype=np.float64)

    # XXX The following is a stopgap measure; we need to set the dimensions
    # of class_log_prior_ and feature_log_prob_ correctly.
    def _get_coef(self):
        return (self.feature_log_prob_[1:]
                if len(self.classes_) == 2 else self.feature_log_prob_)

    def _get_intercept(self):
        return (self.class_log_prior_[1:]
                if len(self.classes_) == 2 else self.class_log_prior_)

    coef_ = property(_get_coef)
    intercept_ = property(_get_intercept)

    def _more_tags(self):
        return {'poor_score': True}


class MultinomialNB(BaseDiscreteNB):
    """
    Naive Bayes classifier for multinomial models

    The multinomial Naive Bayes classifier is suitable for classification with
    discrete features (e.g., word counts for text classification). The
    multinomial distribution normally requires integer feature counts. However,
    in practice, fractional counts such as tf-idf may also work.

    Read more in the :ref:`User Guide <multinomial_naive_bayes>`.

    Parameters
    ----------
    alpha : float, optional (default=1.0)
        Additive (Laplace/Lidstone) smoothing parameter
        (0 for no smoothing).

    fit_prior : boolean, optional (default=True)
        Whether to learn class prior probabilities or not.
        If false, a uniform prior will be used.

    class_prior : array-like, size (n_classes,), optional (default=None)
        Prior probabilities of the classes. If specified the priors are not
        adjusted according to the data.

    Attributes
    ----------
    class_log_prior_ : array, shape (n_classes, )
        Smoothed empirical log probability for each class.

    intercept_ : array, shape (n_classes, )
        Mirrors ``class_log_prior_`` for interpreting MultinomialNB
        as a linear model.

    feature_log_prob_ : array, shape (n_classes, n_features)
        Empirical log probability of features
        given a class, ``P(x_i|y)``.

    coef_ : array, shape (n_classes, n_features)
        Mirrors ``feature_log_prob_`` for interpreting MultinomialNB
        as a linear model.

    class_count_ : array, shape (n_classes,)
        Number of samples encountered for each class during fitting. This
        value is weighted by the sample weight when provided.

    classes_ : array, shape (n_classes,)
        Class labels known to the classifier

    feature_count_ : array, shape (n_classes, n_features)
        Number of samples encountered for each (class, feature)
        during fitting. This value is weighted by the sample weight when
        provided.

    n_features_ : int
        Number of features of each sample.

    classes_ : array-like, shape (n_classes,)
        Unique class labels.

    Examples
    --------
    >>> import numpy as np
    >>> rng = np.random.RandomState(1)
    >>> X = rng.randint(5, size=(6, 100))
    >>> y = np.array([1, 2, 3, 4, 5, 6])
    >>> from sklearn.naive_bayes import MultinomialNB
    >>> clf = MultinomialNB()
    >>> clf.fit(X, y)
    MultinomialNB()
    >>> print(clf.predict(X[2:3]))
    [3]

    Notes
    -----
    For the rationale behind the names `coef_` and `intercept_`, i.e.
    naive Bayes as a linear classifier, see J. Rennie et al. (2003),
    Tackling the poor assumptions of naive Bayes text classifiers, ICML.

    References
    ----------
    C.D. Manning, P. Raghavan and H. Schuetze (2008). Introduction to
    Information Retrieval. Cambridge University Press, pp. 234-265.
    https://nlp.stanford.edu/IR-book/html/htmledition/naive-bayes-text-classification-1.html
    """

    def __init__(self, alpha=1.0, fit_prior=True, class_prior=None):
        self.alpha = alpha
        self.fit_prior = fit_prior
        self.class_prior = class_prior

    def _more_tags(self):
        return {'requires_positive_X': True}

    def _count(self, X, Y):
        """Count and smooth feature occurrences."""
        check_non_negative(X, "MultinomialNB (input X)")
        self.feature_count_ += safe_sparse_dot(Y.T, X)
        self.class_count_ += Y.sum(axis=0)

    def _update_feature_log_prob(self, alpha):
        """Apply smoothing to raw counts and recompute log probabilities"""
        smoothed_fc = self.feature_count_ + alpha
        smoothed_cc = smoothed_fc.sum(axis=1)

        self.feature_log_prob_ = (np.log(smoothed_fc) -
                                  np.log(smoothed_cc.reshape(-1, 1)))

    def _joint_log_likelihood(self, X):
        """Calculate the posterior log probability of the samples X"""
        return (safe_sparse_dot(X, self.feature_log_prob_.T) +
                self.class_log_prior_)


class ComplementNB(BaseDiscreteNB):
    """The Complement Naive Bayes classifier described in Rennie et al. (2003).

    The Complement Naive Bayes classifier was designed to correct the "severe
    assumptions" made by the standard Multinomial Naive Bayes classifier. It is
    particularly suited for imbalanced data sets.

    Read more in the :ref:`User Guide <complement_naive_bayes>`.

    Parameters
    ----------
    alpha : float, optional (default=1.0)
        Additive (Laplace/Lidstone) smoothing parameter (0 for no smoothing).

    fit_prior : boolean, optional (default=True)
        Only used in edge case with a single class in the training set.

    class_prior : array-like, size (n_classes,), optional (default=None)
        Prior probabilities of the classes. Not used.

    norm : boolean, optional (default=False)
        Whether or not a second normalization of the weights is performed. The
        default behavior mirrors the implementations found in Mahout and Weka,
        which do not follow the full algorithm described in Table 9 of the
        paper.

    Attributes
    ----------
    class_log_prior_ : array, shape (n_classes, )
        Smoothed empirical log probability for each class. Only used in edge
        case with a single class in the training set.

    feature_log_prob_ : array, shape (n_classes, n_features)
        Empirical weights for class complements.

    class_count_ : array, shape (n_classes,)
        Number of samples encountered for each class during fitting. This
        value is weighted by the sample weight when provided.

    classes_ : array, shape (n_classes,)
        Class labels known to the classifier

    feature_count_ : array, shape (n_classes, n_features)
        Number of samples encountered for each (class, feature) during fitting.
        This value is weighted by the sample weight when provided.

    n_features_ : int
        Number of features of each sample.

    feature_all_ : array, shape (n_features,)
        Number of samples encountered for each feature during fitting. This
        value is weighted by the sample weight when provided.

    classes_ : array of shape (n_classes,)
        The classes labels.

    Examples
    --------
    >>> import numpy as np
    >>> rng = np.random.RandomState(1)
    >>> X = rng.randint(5, size=(6, 100))
    >>> y = np.array([1, 2, 3, 4, 5, 6])
    >>> from sklearn.naive_bayes import ComplementNB
    >>> clf = ComplementNB()
    >>> clf.fit(X, y)
    ComplementNB()
    >>> print(clf.predict(X[2:3]))
    [3]

    References
    ----------
    Rennie, J. D., Shih, L., Teevan, J., & Karger, D. R. (2003).
    Tackling the poor assumptions of naive bayes text classifiers. In ICML
    (Vol. 3, pp. 616-623).
    https://people.csail.mit.edu/jrennie/papers/icml03-nb.pdf
    """

    def __init__(self, alpha=1.0, fit_prior=True, class_prior=None,
                 norm=False):
        self.alpha = alpha
        self.fit_prior = fit_prior
        self.class_prior = class_prior
        self.norm = norm

    def _more_tags(self):
        return {'requires_positive_X': True}

    def _count(self, X, Y):
        """Count feature occurrences."""
        check_non_negative(X, "ComplementNB (input X)")
        self.feature_count_ += safe_sparse_dot(Y.T, X)
        self.class_count_ += Y.sum(axis=0)
        self.feature_all_ = self.feature_count_.sum(axis=0)

    def _update_feature_log_prob(self, alpha):
        """Apply smoothing to raw counts and compute the weights."""
        comp_count = self.feature_all_ + alpha - self.feature_count_
        logged = np.log(comp_count / comp_count.sum(axis=1, keepdims=True))
        # BaseNB.predict uses argmax, but ComplementNB operates with argmin.
        if self.norm:
            summed = logged.sum(axis=1, keepdims=True)
            feature_log_prob = logged / summed
        else:
            feature_log_prob = -logged
        self.feature_log_prob_ = feature_log_prob

    def _joint_log_likelihood(self, X):
        """Calculate the class scores for the samples in X."""
        jll = safe_sparse_dot(X, self.feature_log_prob_.T)
        if len(self.classes_) == 1:
            jll += self.class_log_prior_
        return jll


class BernoulliNB(BaseDiscreteNB):
    """Naive Bayes classifier for multivariate Bernoulli models.

    Like MultinomialNB, this classifier is suitable for discrete data. The
    difference is that while MultinomialNB works with occurrence counts,
    BernoulliNB is designed for binary/boolean features.

    Read more in the :ref:`User Guide <bernoulli_naive_bayes>`.

    Parameters
    ----------
    alpha : float, optional (default=1.0)
        Additive (Laplace/Lidstone) smoothing parameter
        (0 for no smoothing).

    binarize : float or None, optional (default=0.0)
        Threshold for binarizing (mapping to booleans) of sample features.
        If None, input is presumed to already consist of binary vectors.

    fit_prior : boolean, optional (default=True)
        Whether to learn class prior probabilities or not.
        If false, a uniform prior will be used.

    class_prior : array-like, size=[n_classes,], optional (default=None)
        Prior probabilities of the classes. If specified the priors are not
        adjusted according to the data.

    Attributes
    ----------
    class_log_prior_ : array, shape = [n_classes]
        Log probability of each class (smoothed).

    feature_log_prob_ : array, shape = [n_classes, n_features]
        Empirical log probability of features given a class, P(x_i|y).

    class_count_ : array, shape = [n_classes]
        Number of samples encountered for each class during fitting. This
        value is weighted by the sample weight when provided.

    classes_ : array, shape (n_classes,)
        Class labels known to the classifier

    feature_count_ : array, shape = [n_classes, n_features]
        Number of samples encountered for each (class, feature)
        during fitting. This value is weighted by the sample weight when
        provided.

    n_features_ : int
        Number of features of each sample.

    classes_ : array of shape (n_classes,)
        The classes labels.

    Examples
    --------
    >>> import numpy as np
    >>> rng = np.random.RandomState(1)
    >>> X = rng.randint(5, size=(6, 100))
    >>> Y = np.array([1, 2, 3, 4, 4, 5])
    >>> from sklearn.naive_bayes import BernoulliNB
    >>> clf = BernoulliNB()
    >>> clf.fit(X, Y)
    BernoulliNB()
    >>> print(clf.predict(X[2:3]))
    [3]

    References
    ----------

    C.D. Manning, P. Raghavan and H. Schuetze (2008). Introduction to
    Information Retrieval. Cambridge University Press, pp. 234-265.
    https://nlp.stanford.edu/IR-book/html/htmledition/the-bernoulli-model-1.html

    A. McCallum and K. Nigam (1998). A comparison of event models for naive
    Bayes text classification. Proc. AAAI/ICML-98 Workshop on Learning for
    Text Categorization, pp. 41-48.

    V. Metsis, I. Androutsopoulos and G. Paliouras (2006). Spam filtering with
    naive Bayes -- Which naive Bayes? 3rd Conf. on Email and Anti-Spam (CEAS).
    """

    def __init__(self, alpha=1.0, binarize=.0, fit_prior=True,
                 class_prior=None):
        self.alpha = alpha
        self.binarize = binarize
        self.fit_prior = fit_prior
        self.class_prior = class_prior

    def _check_X(self, X):
        X = super()._check_X(X)
        if self.binarize is not None:
            X = binarize(X, threshold=self.binarize)
        return X

    def _check_X_y(self, X, y):
        X, y = super()._check_X_y(X, y)
        if self.binarize is not None:
            X = binarize(X, threshold=self.binarize)
        return X, y

    def _count(self, X, Y):
        """Count and smooth feature occurrences."""
        self.feature_count_ += safe_sparse_dot(Y.T, X)
        self.class_count_ += Y.sum(axis=0)

    def _update_feature_log_prob(self, alpha):
        """Apply smoothing to raw counts and recompute log probabilities"""
        smoothed_fc = self.feature_count_ + alpha
        smoothed_cc = self.class_count_ + alpha * 2

        self.feature_log_prob_ = (np.log(smoothed_fc) -
                                  np.log(smoothed_cc.reshape(-1, 1)))

    def _joint_log_likelihood(self, X):
        """Calculate the posterior log probability of the samples X"""
        n_classes, n_features = self.feature_log_prob_.shape
        n_samples, n_features_X = X.shape

        if n_features_X != n_features:
            raise ValueError("Expected input with %d features, got %d instead"
                             % (n_features, n_features_X))

        neg_prob = np.log(1 - np.exp(self.feature_log_prob_))
        # Compute  neg_prob · (1 - X).T  as  ∑neg_prob - X · neg_prob
        jll = safe_sparse_dot(X, (self.feature_log_prob_ - neg_prob).T)
        jll += self.class_log_prior_ + neg_prob.sum(axis=1)

        return jll


class CategoricalNB(BaseDiscreteNB):
    """Naive Bayes classifier for categorical features

    The categorical Naive Bayes classifier is suitable for classification with
    discrete features that are categorically distributed. The categories of
    each feature are drawn from a categorical distribution.

    Read more in the :ref:`User Guide <categorical_naive_bayes>`.

    Parameters
    ----------
    alpha : float, optional (default=1.0)
        Additive (Laplace/Lidstone) smoothing parameter
        (0 for no smoothing).

    fit_prior : boolean, optional (default=True)
        Whether to learn class prior probabilities or not.
        If false, a uniform prior will be used.

    class_prior : array-like, size (n_classes,), optional (default=None)
        Prior probabilities of the classes. If specified the priors are not
        adjusted according to the data.

    Attributes
    ----------
    class_log_prior_ : array, shape (n_classes, )
        Smoothed empirical log probability for each class.

    feature_log_prob_ : list of arrays, len n_features
        Holds arrays of shape (n_classes, n_categories of respective feature)
        for each feature. Each array provides the empirical log probability
        of categories given the respective feature and class, ``P(x_i|y)``.

    class_count_ : array, shape (n_classes,)
        Number of samples encountered for each class during fitting. This
        value is weighted by the sample weight when provided.

    category_count_ : list of arrays, len n_features
        Holds arrays of shape (n_classes, n_categories of respective feature)
        for each feature. Each array provides the number of samples
        encountered for each class and category of the specific feature.

    n_features_ : int
        Number of features of each sample.

    Examples
    --------
    >>> import numpy as np
    >>> rng = np.random.RandomState(1)
    >>> X = rng.randint(5, size=(6, 100))
    >>> y = np.array([1, 2, 3, 4, 5, 6])
    >>> from sklearn.naive_bayes import CategoricalNB
    >>> clf = CategoricalNB()
    >>> clf.fit(X, y)
    CategoricalNB()
    >>> print(clf.predict(X[2:3]))
    [3]
    """

    def __init__(self, alpha=1.0, fit_prior=True, class_prior=None):
        self.alpha = alpha
        self.fit_prior = fit_prior
        self.class_prior = class_prior

    def fit(self, X, y, sample_weight=None):
        """Fit Naive Bayes classifier according to X, y

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features. Here, each feature of X is
            assumed to be from a different categorical distribution.
            It is further assumed that all categories of each feature are
            represented by the numbers 0, ..., n - 1, where n refers to the
            total number of categories for the given feature. This can, for
            instance, be achieved with the help of OrdinalEncoder.

        y : array-like, shape = [n_samples]
            Target values.

        sample_weight : array-like, shape = [n_samples], (default=None)
            Weights applied to individual samples (1. for unweighted).

        Returns
        -------
        self : object
        """
        return super().fit(X, y, sample_weight=sample_weight)

    def partial_fit(self, X, y, classes=None, sample_weight=None):
        """Incremental fit on a batch of samples.

        This method is expected to be called several times consecutively
        on different chunks of a dataset so as to implement out-of-core
        or online learning.

        This is especially useful when the whole dataset is too big to fit in
        memory at once.

        This method has some performance overhead hence it is better to call
        partial_fit on chunks of data that are as large as possible
        (as long as fitting in the memory budget) to hide the overhead.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features. Here, each feature of X is
            assumed to be from a different categorical distribution.
            It is further assumed that all categories of each feature are
            represented by the numbers 0, ..., n - 1, where n refers to the
            total number of categories for the given feature. This can, for
            instance, be achieved with the help of OrdinalEncoder.

        y : array-like, shape = [n_samples]
            Target values.

        classes : array-like, shape = [n_classes] (default=None)
            List of all the classes that can possibly appear in the y vector.

            Must be provided at the first call to partial_fit, can be omitted
            in subsequent calls.

        sample_weight : array-like, shape = [n_samples], (default=None)
            Weights applied to individual samples (1. for unweighted).

        Returns
        -------
        self : object
        """
        return super().partial_fit(X, y, classes,
                                   sample_weight=sample_weight)

    def _check_X(self, X):
        # FIXME: we can avoid calling check_array twice after #14872 is merged.
        # X = check_array(X, y, dtype='int', accept_sparse=False,
        #                 force_all_finite=True)
        X = check_array(X, accept_sparse=False, force_all_finite=True)
        X = check_array(X, dtype='int')
        if np.any(X < 0):
            raise ValueError("X must not contain negative values.")
        return X

    def _check_X_y(self, X, y):
        # FIXME: we can avoid calling check_array twice after #14872 is merged.
        # X, y = check_array(X, y, dtype='int', accept_sparse=False,
        #                    force_all_finite=True)
        X, y = check_X_y(X, y, accept_sparse=False, force_all_finite=True)
        X, y = check_X_y(X, y, dtype='int')
        if np.any(X < 0):
            raise ValueError("X must not contain negative values.")
        return X, y

    def _init_counters(self, n_effective_classes, n_features):
        self.class_count_ = np.zeros(n_effective_classes, dtype=np.float64)
        self.category_count_ = [np.zeros((n_effective_classes, 0))
                                for _ in range(n_features)]

    def _count(self, X, Y):
        def _update_cat_count_dims(cat_count, highest_feature):
            diff = highest_feature + 1 - cat_count.shape[1]
            if diff > 0:
                # we append a column full of zeros for each new category
                return np.pad(cat_count, [(0, 0), (0, diff)], 'constant')
            return cat_count

        def _update_cat_count(X_feature, Y, cat_count, n_classes):
            for j in range(n_classes):
                mask = Y[:, j].astype(bool)
                if Y.dtype.type == np.int64:
                    weights = None
                else:
                    weights = Y[mask, j]
                counts = np.bincount(X_feature[mask], weights=weights)
                indices = np.nonzero(counts)[0]
                cat_count[j, indices] += counts[indices]

        self.class_count_ += Y.sum(axis=0)
        for i in range(self.n_features_):
            X_feature = X[:, i]
            self.category_count_[i] = _update_cat_count_dims(
                self.category_count_[i], X_feature.max())
            _update_cat_count(X_feature, Y,
                              self.category_count_[i],
                              self.class_count_.shape[0])

    def _update_feature_log_prob(self, alpha):
        feature_log_prob = []
        for i in range(self.n_features_):
            smoothed_cat_count = self.category_count_[i] + alpha
            smoothed_class_count = smoothed_cat_count.sum(axis=1)
            feature_log_prob.append(
                np.log(smoothed_cat_count) -
                np.log(smoothed_class_count.reshape(-1, 1)))
        self.feature_log_prob_ = feature_log_prob

    def _joint_log_likelihood(self, X):
        if not X.shape[1] == self.n_features_:
            raise ValueError("Expected input with %d features, got %d instead"
                             .format(self.n_features_, X.shape[1]))
        jll = np.zeros((X.shape[0], self.class_count_.shape[0]))
        for i in range(self.n_features_):
            indices = X[:, i]
            jll += self.feature_log_prob_[i][:, indices].T
        total_ll = jll + self.class_log_prior_
        return total_ll
