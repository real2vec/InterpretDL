
import numpy as np
import paddle
from tqdm import tqdm
from .abc_interpreter import Interpreter
from ..data_processor.readers import preprocess_inputs, preprocess_save_path
from ..data_processor.visualizer import explanation_to_vis, show_vis_explanation, save_image


class SmoothGradInterpreter(Interpreter):
    """
    Smooth Gradients Interpreter.

    Smooth Gradients method solves the problem of meaningless local variations in partial derivatives
    by adding random noise to the inputs multiple times and take the average of the
    gradients.

    More details regarding the Smooth Gradients method can be found in the original paper:
    http://arxiv.org/pdf/1706.03825.pdf
    """

    def __init__(self,
                 paddle_model,
                 use_cuda=True,
                 model_input_shape=[3, 224, 224]):
        """
        Initialize the SmoothGradInterpreter.

        Args:
            paddle_model (callable): A paddle model that outputs predictions.
            use_cuda (bool, optional): Whether or not to use cuda. Default: True
            model_input_shape (list, optional): The input shape of the model. Default: [3, 224, 224]
        """
        Interpreter.__init__(self)
        self.paddle_model = paddle_model
        self.model_input_shape = model_input_shape
        self.data_type = 'float32'
        self.paddle_prepared = False

        self.use_cuda = use_cuda
        if not paddle.is_compiled_with_cuda():
            self.use_cuda = False

    def interpret(self,
                  inputs,
                  labels=None,
                  noise_amount=0.1,
                  n_samples=50,
                  visual=True,
                  save_path=None):
        """
        Main function of the interpreter.

        Args:
            inputs (str or list of strs or numpy.ndarray): The input image filepath or a list of filepaths or numpy array of read images.
            labels (list or tuple or numpy.ndarray, optional): The target labels to analyze. The number of labels should be equal to the number of images. If None, the most likely label for each image will be used. Default: None
            noise_amount (float, optional): Noise level of added noise to the image.
                                            The std of Guassian random noise is noise_amount * (x_max - x_min). Default: 0.1
            n_samples (int, optional): The number of new images generated by adding noise. Default: 50
            visual (bool, optional): Whether or not to visualize the processed image. Default: True
            save_path (str or list of strs or None, optional): The filepath(s) to save the processed image(s). If None, the image will not be saved. Default: None

        :return: interpretations/gradients for each image
        :rtype: numpy.ndarray
        """

        imgs, data = preprocess_inputs(inputs, self.model_input_shape)

        bsz = len(data)
        save_path = preprocess_save_path(save_path, bsz)

        data_type = np.array(data).dtype
        self.data_type = data_type

        if not self.paddle_prepared:
            self._paddle_prepare()

        if labels is None:
            _, preds = self.predict_fn(data, None)
            labels = preds

        labels = np.array(labels).reshape((len(imgs), 1))

        max_axis = tuple(np.arange(1, data.ndim))
        stds = noise_amount * (
            np.max(data, axis=max_axis) - np.min(data, axis=max_axis))

        total_gradients = np.zeros_like(data)
        for i in tqdm(range(n_samples), leave=False, position=1):
            noise = np.concatenate([
                np.float32(
                    np.random.normal(0.0, stds[j], (1, ) + tuple(d.shape)))
                for j, d in enumerate(data)
            ])
            data_noised = data + noise
            gradients, _ = self.predict_fn(data_noised, labels)
            total_gradients += gradients

        avg_gradients = total_gradients / n_samples

        # visualization and save image.
        for i in range(len(imgs)):
            # print(imgs[i].shape, avg_gradients[i].shape)
            vis_explanation = explanation_to_vis(imgs[i], np.abs(avg_gradients[i]).sum(0), style='overlay_grayscale')
            if visual:
                show_vis_explanation(vis_explanation)
            if save_path[i] is not None:
                save_image(save_path[i], vis_explanation)

        return avg_gradients

    def _paddle_prepare(self, predict_fn=None):
        if predict_fn is None:
            paddle.set_device('gpu:0' if self.use_cuda else 'cpu')
            # to get gradients, the ``train`` mode must be set.
            self.paddle_model.train()

            for n, v in self.paddle_model.named_sublayers():
                if "batchnorm" in v.__class__.__name__.lower():
                    v._use_global_stats = True
                if "dropout" in v.__class__.__name__.lower():
                    v.p = 0

            def predict_fn(data, labels):
                data = paddle.to_tensor(data)
                data.stop_gradient = False
                out = self.paddle_model(data)
                out = paddle.nn.functional.softmax(out, axis=1)
                preds = paddle.argmax(out, axis=1)
                if labels is None:
                    labels = preds.numpy()
                labels_onehot = paddle.nn.functional.one_hot(
                    paddle.to_tensor(labels), num_classes=out.shape[1])
                target = paddle.sum(out * labels_onehot, axis=1)
                # gradients = paddle.grad(outputs=[target], inputs=[data])[0]
                target.backward()
                gradients = data.grad
                if isinstance(gradients, paddle.Tensor):
                    gradients = gradients.numpy()
                return gradients, labels

        self.predict_fn = predict_fn
        self.paddle_prepared = True
