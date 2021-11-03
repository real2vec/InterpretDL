import abc
import sys
import numpy as np
import warnings


# Ensure compatibility with Python 2/3
if sys.version_info >= (3, 4):
    ABC = abc.ABC
else:
    ABC = abc.ABCMeta(str('ABC'), (), {})


class Interpreter(ABC):
    """Interpreter is the base class for all interpretation algorithms.

    """

    def __init__(self, paddle_model, device, use_cuda, **kwargs):
        """

        Args:
            paddle_model (callable): A model with ``forward`` and possibly ``backward`` functions.
            device (str): The device used for running `paddle_model`, options: ``cpu``, ``gpu:0``, ``gpu:1`` etc.
            use_cuda (bool):  Would be deprecated soon. Use ``device`` directly.
        """
        assert device[:3] in ['cpu', 'gpu']

        if use_cuda in [True, False]:
            warnings.warn(
                '``use_cuda`` would be deprecated soon. '
                'Use ``device`` directly.',
                stacklevel=2
            )
            if use_cuda and device[:3] == 'gpu':
                device = device
            else:
                device = 'cpu'

        self.device = device
        self.paddle_model = paddle_model
        self.predict_fn = None

    def _paddle_prepare(self, predict_fn=None):
        """
        Prepare Paddle program inside of the interpreter. This will be called by interpret().
        Would be renamed to ``_build_predict_fn``.

        Args:
            predict_fn: A defined callable function that defines inputs and outputs.
                Defaults to None, and each interpreter should implement it.
        Returns:
            None

        """
        raise NotImplementedError

    def interpret(self, **kwargs):
        """Main function of the interpreter. 

        Raises:
            NotImplementedError: Should be implemented by interpreters.
        """
        raise NotImplementedError

    def _build_predict_fn(self, **kwargs):
        """Build self.predict_fn for interpreters.

        """
        raise NotImplementedError


class InputGradientInterpreter(Interpreter):
    """Input Gradient based Interpreter.

    Interpreters that are derived from InputGradientInterpreter:
    GradShapCVInterpreter, IntGradCVInterpreter, SmoothGradInterpreter

    """

    def __init__(self, paddle_model, device, use_cuda, **kwargs):
        Interpreter.__init__(self, paddle_model, device, use_cuda, **kwargs)
        assert hasattr(paddle_model, 'forward') and hasattr(paddle_model, 'backward'), \
            "paddle_model has to be " \
            "an instance of paddle.nn.Layer or a compatible one."

    def _build_predict_fn(self, rebuild=False, gradient_of='probability'):
        """Build ``self.predict_fn`` for input gradients based algorithms.
        The model is supposed to be a classification model.

        Args:
            rebuild (bool, optional): forces to rebuid. Defaults to False.
            gradient_of (str, optional): computes the gradient of 
                [``loss``, ``logit`` or ``probability``] w.r.t. input data. 
                Defaults to ``probability``. 
                Other options can get similar results while the absolute 
                scale might be different.
        
        An example of ``predict_fn`` is

        .. code-block:: python

            def predict_fn(data, labels):
                assert len(data.shape) == 4  # [bs, h, w, 3]
                assert labels is None or (isinstance(labels, (list, np.ndarray)) and len(labels) == data.shape[0])

                data = paddle.to_tensor(data)
                data.stop_gradient = False
                logits = self.paddle_model(data)  # get logits, [bs, num_c]
                probas = paddle.nn.functional.softmax(logits, axis=1)  # get probabilities.
                preds = paddle.argmax(probas, axis=1)  # get predictions.
                if labels is None:
                    labels = preds.numpy()  # label is an integer.
                
                if gradient_of == 'loss':
                    # loss
                    loss = paddle.nn.functional.cross_entropy(
                        logits, paddle.to_tensor(labels), reduction='sum'
                    )
                else:
                    # logits or probas
                    labels = np.array(labels).reshape((data.shape[0], ))
                    labels_onehot = paddle.nn.functional.one_hot(
                        paddle.to_tensor(labels), num_classes=probas.shape[1]
                    )
                    if gradient_of == 'logit':
                        loss = paddle.sum(logits * labels_onehot, axis=1)
                    else:
                        loss = paddle.sum(probas * labels_onehot, axis=1)

                loss.backward()
                gradients = data.grad
                if isinstance(gradients, paddle.Tensor):
                    gradients = gradients.numpy()
                return gradients, labels

        """

        if self.predict_fn is not None:
            assert callable(self.predict_fn), "predict_fn is predefined before, but is not callable." \
                "Check it again."

        import paddle
        if self.predict_fn is None or rebuild:
            assert gradient_of in ['loss', 'logit', 'probability']

            if not paddle.is_compiled_with_cuda() and self.device[:3] == 'gpu':
                print("Paddle is not installed with GPU support. Change to CPU version now.")
                self.device = 'cpu'

            # set device. self.device is one of ['cpu', 'gpu:0', 'gpu:1', ...]
            paddle.set_device(self.device)

            # to get gradients, the ``train`` mode must be set.
            self.paddle_model.train()

            # later version will be simplied.
            for n, v in self.paddle_model.named_sublayers():
                if "batchnorm" in v.__class__.__name__.lower():
                    v._use_global_stats = True
                if "dropout" in v.__class__.__name__.lower():
                    v.p = 0

            def predict_fn(data, labels):
                """predict_fn for input gradients based interpreters,
                    for image classification models only.

                Args:
                    data ([type]): scaled input data.
                    labels ([type]): can be None.

                Returns:
                    [type]: gradients, labels
                """
                assert len(data.shape) == 4  # [bs, h, w, 3]
                assert labels is None or \
                    (isinstance(labels, (list, np.ndarray)) and len(labels) == data.shape[0])

                data = paddle.to_tensor(data)
                data.stop_gradient = False
                logits = self.paddle_model(data)  # get logits, [bs, num_c]
                probas = paddle.nn.functional.softmax(logits, axis=1)  # get probabilities.
                preds = paddle.argmax(probas, axis=1)  # get predictions.
                if labels is None:
                    labels = preds.numpy()  # label is an integer.
                
                if gradient_of == 'loss':
                    # loss
                    loss = paddle.nn.functional.cross_entropy(
                        logits, paddle.to_tensor(labels), reduction='sum'
                    )
                else:
                    # logits or probas
                    labels = np.array(labels).reshape((data.shape[0], ))
                    labels_onehot = paddle.nn.functional.one_hot(
                        paddle.to_tensor(labels), num_classes=probas.shape[1]
                    )
                    if gradient_of == 'logit':
                        loss = paddle.sum(logits * labels_onehot, axis=1)
                    else:
                        loss = paddle.sum(probas * labels_onehot, axis=1)

                loss.backward()
                gradients = data.grad
                if isinstance(gradients, paddle.Tensor):
                    gradients = gradients.numpy()
                return gradients, labels

            self.predict_fn = predict_fn


class InputOutputInterpreter(Interpreter):
    """Input-Output Correlation based Interpreter.

    Interpreters that are derived from InputOutputInterpreter:
    OcclusionInterpreter, LIMECVInterpreter

    """

    def __init__(self, paddle_model, device, use_cuda, **kwargs):
        Interpreter.__init__(self, paddle_model, device, use_cuda, **kwargs)
        assert hasattr(paddle_model, 'forward'), \
            "paddle_model has to be " \
            "an instance of paddle.nn.Layer or a compatible one."

    def _build_predict_fn(self, rebuild=False, output='probability'):
        """Build self.predict_fn for Input-Output based algorithms.
        The model is supposed to be a classification model.

        An example of ``predict_fn`` is

        .. code-block:: python

            def predict_fn(data, labels):
                assert len(data.shape) == 4  # [bs, h, w, 3]

                logits = self.paddle_model(paddle.to_tensor(data))  # get logits, [bs, num_c]
                probas = paddle.nn.functional.softmax(logits, axis=1)  # get probabilities.
                preds = paddle.argmax(probas, axis=1)  # get predictions.
                if label is None:
                    label = preds.numpy()  # label is an integer.
                
                if output == 'logit':
                    return logits.numpy(), label
                else:
                    return probas.numpy(), label

        Args:
            rebuild (bool, optional): forces to rebuid. Defaults to False.
            output (str, optional): computes the logit or probability. 
                Defaults to ``probability``. Other options can get similar 
                results while the absolute scale might be different.
        """

        if self.predict_fn is not None:
            assert callable(self.predict_fn), "predict_fn is predefined before, but is not callable." \
                "Check it again."

        import paddle
        if self.predict_fn is None or rebuild:
            assert output in ['logit', 'probability']

            if not paddle.is_compiled_with_cuda() and self.device[:3] == 'gpu':
                print("Paddle is not installed with GPU support. Change to CPU version now.")
                self.device = 'cpu'

            # set device. self.device is one of ['cpu', 'gpu:0', 'gpu:1', ...]
            paddle.set_device(self.device)

            # to get gradients, the ``train`` mode must be set.
            self.paddle_model.eval()

            def predict_fn(data, label):
                """predict_fn for input gradients based interpreters,
                    for image classification models only.

                Args:
                    data ([type]): [description]
                    label ([type]): can be None.

                Returns:
                    [type]: [description]
                """
                assert len(data.shape) == 4  # [bs, h, w, 3]

                logits = self.paddle_model(paddle.to_tensor(data))  # get logits, [bs, num_c]
                probas = paddle.nn.functional.softmax(logits, axis=1)  # get probabilities.
                preds = paddle.argmax(probas, axis=1)  # get predictions.
                if label is None:
                    label = preds.numpy()  # label is an integer.
                
                if output == 'logit':
                    return logits.numpy(), label
                else:
                    return probas.numpy(), label

            self.predict_fn = predict_fn


class IntermediateLayerInterpreter(Interpreter):
    """Interpreter that exhibits features from intermediate layers to produce explanations.
    This interpreter extracts one layer's feature.

    Interpreters that are derived from IntermediateLayerInterpreter:
    ScoreCAMInterpreter

    """

    def __init__(self, paddle_model, device, use_cuda, **kwargs):
        Interpreter.__init__(self, paddle_model, device, use_cuda, **kwargs)
        assert hasattr(paddle_model, 'forward'), \
            "paddle_model has to be " \
            "an instance of paddle.nn.Layer or a compatible one."

    def _build_predict_fn(self, rebuild=False, target_layer=None):
        """Build self.predict_fn for IntermediateLayer based algorithms.
        The model is supposed to be a classification model.
        An example of ``predict_fn`` is

        .. code-block:: python

            def predict_fn(data, labels):
                target_feature_map = []
                
                def hook(layer, input, output):
                    target_feature_map.append(output)

                hooks = []
                for name, v in self.paddle_model.named_sublayers():
                    if name == target_layer:
                        h = v.register_forward_post_hook(hook)
                        hooks.append(h)

                assert len(hooks) == 1, f"target_layer `{target_layer}`` does not exist in the given model, \n \
                                the list of layer names are \n \
                                {[n for n, v in self.paddle_model.named_sublayers()]}"
                
                with paddle.no_grad():
                    data = paddle.to_tensor(data)
                    logits = self.paddle_model(data)

                    # has to be removed.
                    for h in hooks:
                        h.remove()
                    
                    probas = paddle.nn.functional.softmax(logits, axis=1)
                    predict_label = paddle.argmax(probas, axis=1)  # get predictions.
                return target_feature_map[0].numpy(), probas.numpy(), predict_label.numpy()

        Args:
            rebuild (bool, optional): forces to rebuid. Defaults to False.
            target_layer (str, optional): given the name of the target layer at which
                the explanation is computed.
        """

        if self.predict_fn is not None:
            assert callable(self.predict_fn), "predict_fn is predefined before, but is not callable." \
                "Check it again."

        import paddle
        if self.predict_fn is None or rebuild:
            assert target_layer is not None, '``target_layer`` has to be given.'

            if not paddle.is_compiled_with_cuda() and self.device[:3] == 'gpu':
                print("Paddle is not installed with GPU support. Change to CPU version now.")
                self.device = 'cpu'

            # set device. self.device is one of ['cpu', 'gpu:0', 'gpu:1', ...]
            paddle.set_device(self.device)

            # to get gradients, the ``train`` mode must be set.
            self.paddle_model.eval()

            def predict_fn(data):

                target_feature_map = []
                def hook(layer, input, output):
                    target_feature_map.append(output)

                hooks = []
                for name, v in self.paddle_model.named_sublayers():
                    if name == target_layer:
                        h = v.register_forward_post_hook(hook)
                        hooks.append(h)

                assert len(hooks) == 1, f"target_layer `{target_layer}`` does not exist in the given model, \
                                the list of layer names are \n \
                                {[n for n, v in self.paddle_model.named_sublayers()]}"
                
                with paddle.no_grad():
                    data = paddle.to_tensor(data)
                    logits = self.paddle_model(data)

                    # has to be removed.
                    for h in hooks:
                        h.remove()
                    
                    probas = paddle.nn.functional.softmax(logits, axis=1)
                    predict_label = paddle.argmax(probas, axis=1)  # get predictions.
                return target_feature_map[0].numpy(), probas.numpy(), predict_label.numpy()

            self.predict_fn = predict_fn
