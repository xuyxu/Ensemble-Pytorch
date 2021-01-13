"""
  Adversarial training is able to improve the performance of an ensemble by
  treating adversarial samples as the augmented training data. The fast
  gradient sign method (FGSM) is used to generate adversarial samples.

  Reference:
      B. Lakshminarayanan, A. Pritzel, C. Blundell., Simple and Scalable
      Predictive Uncertainty Estimation using Deep Ensembles, NIPS 2017.
"""


import torch
import torch.nn as nn
import torch.nn.functional as F
from joblib import Parallel, delayed

from ._base import BaseModule, torchensemble_model_doc
from . import utils


__fit_doc = """
    Parameters
    ----------
    train_loader : torch.utils.data.DataLoader
        A :mod:`DataLoader` container that contains the training data.
    lr : float, default=1e-3
        The learning rate of the parameter optimizer.
    weight_decay : float, default=5e-4
        The weight decay of the parameter optimizer.
    epochs : int, default=100
        The number of training epochs.
    optimizer : {"SGD", "Adam", "RMSprop"}, default="Adam"
        The type of parameter optimizer.
    epsilon : float, defaul=0.01
        The step used to generate adversarial samples in the fast gradient
        sign method (FGSM).
    log_interval : int, default=100
        The number of batches to wait before printting the training status.
    test_loader : torch.utils.data.DataLoader, default=None
        A :mod:`DataLoader` container that contains the evaluating data.

        - If ``None``, no validation is conducted after each training
          epoch.
        - If not ``None``, the ensemble will be evaluated on this
          dataloader after each training epoch.
    save_model : bool, default=True
        Whether to save the model.

        - If test_loader is ``None``, the ensemble containing
          ``n_estimators`` base estimators will be saved.
        - If test_loader is not ``None``, the ensemble with the best
          validation performance will be saved.
    save_dir : string, default=None
        Specify where to save the model.

        - If ``None``, the model will be saved in the current directory.
        - If not ``None``, the model will be saved in the specified
          directory: ``save_dir``.
"""


def _adversarial_training_model_doc(header, item="fit"):
    """
    Decorator on obtaining documentation for different adversarial training
    models.
    """
    def get_doc(item):
        """Return selected item"""
        __doc = {"fit": __fit_doc}
        return __doc[item]

    def adddoc(cls):
        doc = [header + "\n\n"]
        doc.extend(get_doc(item))
        cls.__doc__ = "".join(doc)
        return cls
    return adddoc


def _parallel_fit_per_epoch(train_loader,
                            lr,
                            weight_decay,
                            epoch,
                            optimizer,
                            epsilon,
                            log_interval,
                            idx,
                            estimator,
                            criterion,
                            device,
                            is_classification):
    """Private function used to fit base estimators in parallel."""
    optimizer = utils.set_optimizer(estimator, optimizer, lr, weight_decay)

    for batch_idx, (data, target) in enumerate(train_loader):

        batch_size = data.size()[0]
        data, target = data.to(device), target.to(device)
        data.requires_grad = True

        optimizer.zero_grad()

        output = estimator(data)
        loss = criterion(output, target)

        loss.backward()

        # Also compute the adversarial loss
        data_grad = data.grad.data
        adv_data = _get_fgsm_samples(data, epsilon, data_grad)
        adv_output = estimator(adv_data)
        adv_loss = criterion(adv_output, target)
        adv_loss.backward()

        optimizer.step()

        # Print training status
        if batch_idx % log_interval == 0:

            # Classification
            if is_classification:
                pred = output.data.max(1)[1]
                correct = pred.eq(target.view(-1).data).sum()

                msg = ("Estimator: {:03d} | Epoch: {:03d} | Batch: {:03d}"
                       " | Loss: {:.5f} | Correct: {:d}/{:d}")
                print(
                    msg.format(
                        idx, epoch, batch_idx, loss, correct, batch_size
                    )
                )
            # Regression
            else:
                msg = ("Estimator: {:03d} | Epoch: {:03d} | Batch: {:03d}"
                       " | Loss: {:.5f}")
                print(msg.format(idx, epoch, batch_idx, loss))

    return estimator


def _get_fgsm_samples(sample, epsilon, sample_grad):
    """
    Private functions used to generate adversarial samples with fast gradient
    sign method (FGSM)."""
    sign_sample_grad = sample_grad.sign()
    perturbed_sample = sample + epsilon*sign_sample_grad
    perturbed_sample = torch.clamp(perturbed_sample, 0, 1)

    return perturbed_sample


class _BaseAdversarialTraining(BaseModule):

    def _validate_parameters(self,
                             lr,
                             weight_decay,
                             epochs,
                             epsilon,
                             log_interval):
        """Validate hyper-parameters on training the ensemble."""

        if not lr > 0:
            msg = ("The learning rate of optimizer = {} should be strictly"
                   " positive.")
            self.logger.error(msg.format(lr))
            raise ValueError(msg.format(lr))

        if not weight_decay >= 0:
            msg = "The weight decay of optimizer = {} should not be negative."
            self.logger.error(msg.format(weight_decay))
            raise ValueError(msg.format(weight_decay))

        if not epochs > 0:
            msg = ("The number of training epochs = {} should be strictly"
                   " positive.")
            self.logger.error(msg.format(epochs))
            raise ValueError(msg.format(epochs))

        if not 0 < epsilon <= 1:
            msg = ("The step used to generate adversarial samples in FGSM"
                   " should be in the range (0, 1], but got {} instead.")
            self.logger.error(msg.format(epsilon))
            raise ValueError(msg.format(epsilon))

        if not log_interval > 0:
            msg = ("The number of batches to wait before printting the"
                   " training status should be strictly positive, but got {}"
                   " instead.")
            self.logger.error(msg.format(log_interval))
            raise ValueError(msg.format(log_interval))


@torchensemble_model_doc("""Implementation on the AdversarialTrainingClassifier.""",  # noqa: E501
                         "model")
class AdversarialTrainingClassifier(_BaseAdversarialTraining):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_classification = True

    @torchensemble_model_doc(
        """Implementation on the data forwarding in AdversarialTrainingClassifier.""",  # noqa: E501
        "classifier_forward")
    def forward(self, X):
        batch_size = X.size()[0]
        proba = torch.zeros(batch_size, self.n_outputs).to(self.device)

        # Take the average over class distributions from all base estimators.
        for estimator in self.estimators_:
            proba += F.softmax(estimator(X), dim=1) / self.n_estimators

        return proba

    @_adversarial_training_model_doc(
        """Implementation on the training stage of AdversarialTrainingClassifier.""",  # noqa: E501
        "fit"
    )
    def fit(self,
            train_loader,
            lr=1e-3,
            weight_decay=5e-4,
            epochs=100,
            optimizer="Adam",
            epsilon=0.01,
            log_interval=100,
            test_loader=None,
            save_model=True,
            save_dir=None):

        # Instantiate base estimators and set attributes
        estimators = []
        for _ in range(self.n_estimators):
            estimators.append(self._make_estimator())
        self.n_outputs = self._decide_n_outputs(train_loader, True)
        self._validate_parameters(lr,
                                  weight_decay,
                                  epochs,
                                  epsilon,
                                  log_interval)

        self.train()

        # Utils
        criterion = nn.CrossEntropyLoss()
        best_acc = 0.

        # Internal helper function on pesudo forward
        def _forward(estimators, data):
            batch_size = data.size()[0]
            proba = torch.zeros(batch_size, self.n_outputs).to(self.device)

            for estimator in estimators:
                proba += F.softmax(estimator(data), dim=1) / self.n_estimators

            return proba

        # Maintain a pool of workers
        with Parallel(n_jobs=self.n_jobs) as parallel:

            # Training loop
            for epoch in range(epochs):
                rets = parallel(delayed(_parallel_fit_per_epoch)(
                        train_loader,
                        lr,
                        weight_decay,
                        epoch,
                        optimizer,
                        epsilon,
                        log_interval,
                        idx,
                        estimator,
                        criterion,
                        self.device,
                        True
                    )
                    for idx, estimator in enumerate(estimators)
                )

                estimators = rets  # update

                # Validation
                if test_loader:
                    with torch.no_grad():
                        correct = 0.
                        for _, (data, target) in enumerate(test_loader):
                            data, target = (data.to(self.device),
                                            target.to(self.device))
                            output = _forward(estimators, data)
                            pred = output.data.max(1)[1]
                            correct += pred.eq(target.view(-1).data).sum()
                        acc = 100. * float(correct) / len(test_loader.dataset)

                        if acc > best_acc:
                            best_acc = acc
                            self.estimators_ = nn.ModuleList()  # reset
                            self.estimators_.extend(estimators)
                            if save_model:
                                utils.save(self, save_dir, self.logger)

                        msg = ("Epoch: {:03d} | Validation Acc: {:.3f}"
                               " % | Historical Best: {:.3f} %")
                        self.logger.info(msg.format(epoch, acc, best_acc))

        self.estimators_ = nn.ModuleList()
        self.estimators_.extend(rets)
        if save_model and not test_loader:
            utils.save(self, save_dir, self.logger)

    @torchensemble_model_doc(
        """Implementation on the evaluating stage of AdversarialTrainingClassifier.""",  # noqa: E501
        "classifier_predict")
    def predict(self, test_loader):
        self.eval()
        correct = 0

        for batch_idx, (data, target) in enumerate(test_loader):
            data, target = data.to(self.device), target.to(self.device)
            output = self.forward(data)
            pred = output.data.max(1)[1]
            correct += pred.eq(target.view(-1).data).sum()

        accuracy = 100. * float(correct) / len(test_loader.dataset)

        return accuracy


class AdversarialTrainingRegressor(_BaseAdversarialTraining):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_classification = False
        self.criterion = nn.MSELoss()

    def forward(self, X):
        pred = self._forward(X)

        return pred
