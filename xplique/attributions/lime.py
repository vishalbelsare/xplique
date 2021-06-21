"""
Module related to LIME method
"""

import warnings

import tensorflow as tf
from sklearn import linear_model
from skimage.segmentation import quickshift

from .base import BlackBoxExplainer
from ..utils import sanitize_input_output

class Lime(BlackBoxExplainer):
    """
    Used to compute the LIME method.

    Ref. Ribeiro & al., "Why Should I Trust You?": Explaining the Predictions of Any Classifier.
    https://arxiv.org/abs/1602.04938

    Note that the quality of an explanation relies strongly on your choice of the interpretable
    model, the similarity kernel and the map function mapping a sample into an interpretable space.
    The similarity kernel will define how close the pertubed samples are from the original sample
    you want to explain.
    For instance, if you have large images (e.g 299x299x3) the default similarity kernel with the
    default kernel width will compute similarities really close to 0 consequently the interpretable
    model will not train. In order to makes it work you have to use (for example) a larger kernel
    width.
    Moreover, depending on the similarities vector you obtain some interpretable model will fit
    better than other (e.g Ridge on large colored image might perform better than Lasso).
    Finally, your map function will defines how many features your linear model has to learn.
    Basically, the default mapping is an identity mapping, so for large image it means there is
    as many features as there are pixels (e.g 299x299x3->89401 features) which can lead to poor
    explanations.

    N.B: This module was built to be deployed on GPU to be fully efficient. Considering the number
    of samples and number of inputs you want to process it might even be necessary.
    """
    def __init__(self,
                 model,
                 batch_size: int = 1,
                 interpretable_model = linear_model.Ridge(alpha=2),
                 similarity_kernel = None,
                 pertub_func = None,
                 map_to_interpret_space = None,
                 ref_values = None,
                 nb_samples: int = 150,
                 batch_pertubed_samples = None,
                 distance_mode: str = "euclidean",
                 kernel_width: float = 45.0,
                 prob: float = 0.5
                 ): # pylint: disable=R0913
        """
        Parameters
        ----------
        model : tf.keras.Model
            Model that you want to explain.

        batch_size : int, optional
            Number of samples to explain at once, if None compute all at once.

        interpretable_model : Model, optional
            Model object to train interpretable model.
            A Model object provides a `fit` method to train the model,
            containing three array:

            - interpretable_inputs: ndarray (2D nb_samples x num_interp_features),
            - expected_outputs: ndarray (1D nb_samples),
            - weights: ndarray (1D nb_samples)

            The model object should also provide a `predict` method and have a coef_ attributes
            (the interpretable explanation).
            As interpretable model you can use linear models from scikit-learn.
            Note that here nb_samples doesn't indicates the length of inputs but the number of
            pertubed samples we want to generate for each input.

        similarity_kernel : callable, optional
            Function which takes a pertubed sample and returned the similarity with the original
            sample.
            The similarity can be computed in the original input space or in the interpretable
            space.
            You can provide a custom function. Note that to use a custom function, you have to
            follow the following scheme:

            @tf.function
            def custom_similarity(curr_input , sample, interpret_sample) ->
            tf.tensor (shape=(1,), dtype = tf.float32):
                ** some tf actions **
                return similarity

            where:
                curr_input, sample are tf.tensor (W, H, C)
                interpretable sample is a tf.tensor (num_interp_features)
            The default similarity kernel use the euclidian distance between the original input and
            sample in the input space.

        pertub_function : callable, optional
            Function which generate pertubed interpretable samples in the interpretation space from
            the number of interpretable features (e.g nb of super pixel) and the number of pertubed
            samples you want per original sample.
            The generated interp_samples belong to {0,1}^num_features. Where 1 indicates that we
            keep the corresponding feature (e.g super pixel) in the mapping.
            To use your own custom pertub function you should use the following scheme:

            @tf.function
            def custom_pertub_function(num_features, nb_samples) ->
            tf.tensor (shape=(nb_samples, num_interp_features), dtype=tf.int32):
                ** some tf actions**
                return pertubed_sample

            The default pertub function provided keep a feature (e.g super pixel) with a
            probability 0.5.
            If you want to change it, defines your own prob value when initiating the explainer.

        ref_values : ndarray
            It defines reference value which replaces each feature when the corresponding
            interpretable feature is set to 0.
            It should be provided as: a ndarray (C,)

            The default ref value is set to (0.5,0.5,0.5) for inputs with 3 channels (corresponding
            to a grey pixel when inputs are normalized by 255) and to 0 otherwise.

        map_to_interpret_space : callable, optional
            Function which group an input features which correspond to the same interpretable
            feature (e.g super-pixel).
            It allows to transpose from (resp. to) the original input space to (resp. from)
            the interpretable space.
            The default mapping is the quickshift segmentation algorithm.

            To use your own custom map function you should use the following scheme:

            def custom_map_to_interpret_space(inputs: tf.tensor (N, W, H, C)) ->
            tf.tensor (N, W, H):
                **some grouping techniques**
                return mappings

            For instance you can use the scikit-image (as we did for the quickshift algorithm)
            library to defines super pixels on your images.

        nb_samples: int
            The number of pertubed samples you want to generate for each input sample.
            Default to 150.

        batch_pertubed_samples: int
            The batch size to predict the pertubed samples labels value.
            Default to None (i.e predictions of all the pertubed samples one shot).

        prob:
            The probability argument for the default pertub function.

        distance_mode:
            The distance mode used in the default similarity kernel, you can choose either euclidean
            or cosine (will compute cosine similarity). Default value set to euclidean.

        kernel_width:
            Width of your kernel. It is important to make it evolving depending on your inputs size
            otherwise you will get all similarity close to 0 leading to poor performance or NaN
            values.
            Default to 1.
        """

        if similarity_kernel is None:
            similarity_kernel = Lime._get_exp_kernel_func(distance_mode, kernel_width)

        if pertub_func is None:
            pertub_func = Lime._get_default_pertub_function(prob)

        if map_to_interpret_space is None:
            map_to_interpret_space = Lime._default_map_to_interpret_space

        if (nb_samples>=500) and (batch_pertubed_samples is None):
            warnings.warn(
                "You set a number of pertubed samples per input >= 500 and "
                "batch_pertubed_samples is set to None"
                "This mean that you will ask your model to make more than 500 predictions"
                " one shot."
                "This can lead to OOM issue. To avoid it you can set the"
                " batch_pertubed_samples."
            )

        super().__init__(model, batch_size)

        self.map_to_interpret_space = map_to_interpret_space
        self.interpretable_model = interpretable_model
        self.similarity_kernel = similarity_kernel
        self.pertub_func = pertub_func
        self.ref_values = ref_values
        self.nb_samples = nb_samples
        self.batch_pertubed_samples = batch_pertubed_samples

    @sanitize_input_output
    def explain(self, inputs, labels):
        """
        This method attributes the output of the model with given labels
        to the inputs of the model using the approach described above,
        training an interpretable model and returning a representation of the
        interpretable model.

        Parameters
        ----------
        inputs : ndarray (N, W, H, C)
            Input samples, with N number of samples, W & H the sample dimensions, and C the
            number of channels.

        labels : ndarray (N, L)
            One hot encoded labels to compute for each sample, with N the number of samples,
            and L the number of classes.

        Returns
        -------
        explanations : ndarray (N, W, H)
            Coefficients of the interpretable model. Those coefficients having the size of the
            interpretable space will be given the same value to coefficient which were grouped
            together (e.g belonging to the same super-pixel).
        """

        if self.ref_values is None:
            if inputs.shape[-1] == 3:
                # grey pixel
                ref_values = tf.ones(inputs.shape[-1])*0.5
            else:
                ref_values = tf.zeros(inputs.shape[-1])
        else:
            assert(
                self.ref_values.shape[0] == inputs.shape[-1]
            ),"The dimension of ref_values must match inputs (C, )"
            ref_values = tf.cast(self.ref_values, tf.float32)

        # use the map function to get a mapping per input to the interpretable space
        mappings = self.map_to_interpret_space(inputs)

        return Lime._compute(self.model,
                            self.batch_size,
                            inputs,
                            labels,
                            self.interpretable_model,
                            self.similarity_kernel,
                            self.pertub_func,
                            ref_values,
                            mappings,
                            self.nb_samples,
                            self.batch_pertubed_samples
                            )

    @staticmethod
    def _compute(model,
                batch_size,
                inputs,
                labels,
                interpretable_model,
                similarity_kernel,
                pertub_func,
                ref_values,
                mappings,
                nb_samples,
                batch_pertubed_samples
                ): # pylint: disable=R0913
        """
        This method attributes the output of the model with given labels
        to the inputs of the model using the approach described above,
        training an interpretable model and returning a representation of the
        interpretable model.

        Parameters
        ----------
        model : tf.keras.Model
            Model to explain.

        inputs : tf.tensor (N, W, H, C)
            Input samples, with N number of samples, W & H the sample dimensions, and C the
            number of channels.

        labels : tf.tensor (N, L)
            One hot encoded labels to compute for each sample, with N the number of samples,
            and L the number of classes.

        interpretable_model : Model
            Model object to train interpretable model.
            A Model object provides a `fit` method to train the model,
            containing three array:

            - interpretable_inputs: ndarray (2D nb_samples x num_interp_features),
            - expected_outputs: ndarray (1D nb_samples),
            - weights: ndarray (1D nb_samples)

            The model object should also provide a `predict` method and have a coef_ attributes
            (the interpretable explanation).
            As interpretable model you can use linear models from scikit-learn.
            Note that here nb_samples doesn't indicates the length of inputs but the number of
            pertubed samples we want to generate for each input.

        similarity_kernel : callable
            Function which takes a pertubed sample and returned the similarity with the original
            sample.
            The similarity can be computed in the original input space or in the interpretable
            space.
            The function has to follow the following scheme:

            @tf.function
            def custom_similarity(curr_input , sample, interpret_sample) ->
            tf.tensor (shape=(1,), dtype = tf.float32):
                ** some tf actions **
                return similarity

            where:
                curr_input, sample are tf.tensor (W, H, C)
                interpretable sample is a tf.tensor (num_interp_features)

        pertub_function : callable
            Function which generate pertubed interpretable samples in the interpretation space from
            the number of interpretable features (e.g nb of super pixel) and the number of pertubed
            samples you want per original sample.
            The generated interp_samples belong to {0,1}^num_features. Where 1 indicates that we
            keep the corresponding feature (e.g super pixel) in the mapping.
            The pertub function should use the following scheme:

            @tf.function
            def custom_pertub_function(num_features, nb_samples) ->
            tf.tensor (shape=(nb_samples, num_interp_features), dtype=tf.int32):
                ** some tf actions**
                return pertubed_sample

        ref_values : ndarray
            It defines reference value which replaces each feature when the corresponding
            interpretable feature is set to 0.
            It should be provided as: a ndarray (C,)

            The default ref value is set to (0.5,0.5,0.5) for inputs with 3 channels (corresponding
            to a grey pixel when inputs are normalized by 255) and to 0 otherwise.

        mappings: tf.tensor (N, W, H)
            It is grouping features which correspond to the same interpretable feature (super-pixel)
            It allows to transpose from (resp. to) the original input space to (resp. from) the
            interpretable space.

            Values accross all tensors should be integers in the range 0 to num_interp_features - 1

        nb_samples: int
            The number of pertubed samples you want to generate for each input sample.
            Default to 150.

        batch_pertubed_samples: int
            The batch size to predict the pertubed samples labels value.
            Default to None (i.e predictions of all the pertubed samples one shot).

        Returns
        -------
        explanations : tf.tensor (N, W, H)
            Coefficients of the interpretable model. Those coefficients having the size of the
            interpretable space will be given the same value to coefficient which were grouped
            together (e.g belonging to the same super-pixel).
        """
        explanations = []

        # get the number of interpretable features for each inputs
        num_features = tf.reduce_max(tf.reduce_max(mappings, axis=1),axis=1)
        num_features += tf.ones(len(mappings),dtype=tf.int32)

        if tf.greater(tf.cast(tf.reduce_max(num_features),tf.float32),1e4):
            warnings.warn(
                "One or several inputs got a number of interpretable features > 10000. "
                "This can be very slow or lead to OOM issues when fitting the interpretable"
                "model. You should consider using a map function which select less features."
            )

        # augment the label vector to match (N, nb_samples, L)
        augmented_labels = tf.expand_dims(labels, axis=1)
        augmented_labels = tf.repeat(augmented_labels, repeats=nb_samples, axis=1)

        # add a prefetch variable for numerous inputs
        nb_prefetch = 0
        if len(inputs)//batch_size > 2:
            nb_prefetch = 2

        # batch inputs, mappings, augmented labels and num_features
        for b_inp, b_labels, b_mappings, b_num_features in tf.data.Dataset.from_tensor_slices(
            (inputs, augmented_labels, mappings, num_features)
        ).batch(batch_size).prefetch(nb_prefetch):

            # get the pertubed samples (interpretable and in the original space)
            interpret_samples, pertubed_samples = tf.map_fn(
                fn= lambda inp: Lime._generate_sample(
                    inp[0],
                    pertub_func,
                    inp[1],
                    inp[2],
                    nb_samples,
                    ref_values
                ),
                elems=(b_inp, b_mappings, b_num_features),
                fn_output_signature=(tf.int32, tf.float32)
            )

            # get the labels of pertubed_samples
            samples_labels = tf.map_fn(
                fn= lambda inp: Lime._batch_predictions(
                    model,
                    inp[0],
                    inp[1],
                    batch_pertubed_samples
                ),
                elems=(pertubed_samples, b_labels),
                fn_output_signature=tf.float32
            )

            # compute similiraty between original inputs and their pertubed versions
            similarities = tf.map_fn(
                fn= lambda inp: Lime._compute_similarities(
                    inp[0],
                    inp[1],
                    inp[2],
                    similarity_kernel
                ),
                elems=(b_inp, pertubed_samples, interpret_samples),
                fn_output_signature=tf.float32
            )

            # train the interpretable model
            for int_samples, samples_label, samples_weight in tf.data.Dataset.from_tensor_slices(
                    (interpret_samples,samples_labels,similarities)):

                explain_model = interpretable_model

                explain_model.fit(int_samples.numpy(),
                                    samples_label.numpy(),
                                    sample_weight=samples_weight.numpy())

                explanation = explain_model.coef_
                # add the interpretable explanation
                explanation = tf.cast(explanation, dtype=tf.float32)
                explanations.append(explanation)

        explanations = tf.stack(explanations, axis=0)
        # broadcast explanations to match the original inputs shapes
        complete_explanations = tf.map_fn(
            fn= lambda inp: Lime._broadcast_explanation(inp[0],inp[1]),
            elems=(explanations,mappings),
            fn_output_signature=tf.float32
        )

        return complete_explanations

    @staticmethod
    def _default_map_to_interpret_space(inputs):
        """
        This method compute the quickshift segmentation.

        Parameters
        ----------
        inputs: tf.tensor (N, W, H, C)
            Input samples, with N number of samples, W & H the sample dimensions, and C the
            number of channels.

        Returns
        -------
        mappings: tf.tensor (N, W, H)
            Mappings which map each pixel to the corresponding segment
        """
        mappings = []
        for inp in inputs:
            mapping = quickshift(inp.numpy().astype('double'), ratio=0.5, kernel_size=2)
            mapping = tf.cast(mapping, tf.int32)
            mappings.append(mapping)
        mappings = tf.stack(mappings, axis=0)
        return mappings

    @staticmethod
    def _get_default_pertub_function(prob: float = 0.5):
        """
        This method allows you to get a pertub function with the corresponding prob
        argument.
        """

        prob = tf.cast(prob, dtype=tf.float32)
        @tf.function
        def _default_pertub_function(num_features, nb_samples):
            """
            This method generate nb_samples tensor belonging to {0,1}^num_features.
            The prob argument is the probability to have a 1.

            Parameters
            ----------
            num_features : int
                The number of interpretable features (e.g super pixel).
            nb_samples : int
                The number of pertubed interpretable samples we want
            prob:
                It defines the probability to draw a 1

            Returns
            -------
            interpretable_pertubed_samples : tf.tensor (nb_samples, num_features)
            """
            probs = tf.ones([1,num_features],tf.float32)*tf.cast(prob,tf.float32)
            uniform_sampling = tf.random.uniform(shape=[nb_samples,num_features],
                                                dtype=tf.float32,
                                                minval=0,
                                                maxval=1)
            sample = tf.greater(probs, uniform_sampling)
            sample = tf.cast(sample, dtype=tf.int32)
            return sample

        return _default_pertub_function

    @staticmethod
    @tf.function
    def _get_masks(interpret_samples, mapping):
        """
        This method translate the generated samples in the interpretable space of an input into
        masks to apply to the original input to obtain samples in the original input space.

        Parameters
        ----------
        interpret_samples : tf.tensor (nb_samples, num_features)
            Intrepretable samples of an input, with:
                nb_samples number of samples
                num_features the dimension of the interpretable space.
        mapping : tf.tensor (W, H)
            The mapping of the original input from which we drawn interpretable samples.
            Its size is equal to width and height of the original input

        Returns
        -------
        masks : tf.tensor (nb_samples, W, H)
            The masks corresponding to each interpretable samples
        """
        tf_masks = tf.gather(interpret_samples,indices=mapping,axis=1)
        return tf_masks

    @staticmethod
    @tf.function
    def _apply_masks(original_input, sample_masks, ref_value):
        """
        This method apply masks obtained from the pertubed interpretable samples to the
        original input (i.e we get pertubed samples in the original space).

        Parameters
        ----------
        original_input : tf.tensor (W, H, C)
            The input we want to explain
        sample_masks : tf.tensor (nb_samples, W, H)
            The masks we obtained from the pertubed instances in the interpretable space
        ref_value : tf.tensor (C)
            The reference value which replaces each feature when the corresponding
            interpretable feature is set to 0

        Returns
        -------
        pertubed_samples : tf.tensor (nb_samples, W, H, C)
            The pertubed samples corresponding to the masks applied to the original input
        """
        pert_samples = tf.expand_dims(original_input, axis=0)
        pert_samples = tf.repeat(pert_samples, repeats=len(sample_masks), axis=0)

        sample_masks = tf.expand_dims(sample_masks, axis=-1)
        sample_masks = tf.repeat(sample_masks, repeats=original_input.shape[-1], axis=-1)

        pert_samples = pert_samples * tf.cast(sample_masks, tf.float32)
        ref_val = tf.reshape(ref_value, (1,1,1,original_input.shape[-1]))
        pert_samples += (tf.ones((sample_masks.shape)) - tf.cast(sample_masks, tf.float32))*ref_val

        return pert_samples


    @staticmethod
    @tf.function
    def _generate_sample(original_input,
                        pertub_func,
                        mapping,
                        num_features,
                        nb_samples,
                        ref_value):
        """
        This method generate nb_samples pertubed instance of the current input in the
        interpretable space.
        Then it computes the pertubed instances into the input space.

        Parameters
        ----------
        original_input : tf.tensor (W, H, C)
            The input we want to explain
        pertub_func: callable
            Function which generate a pertubed sample in the interpretation space from an
            interpretable input.
        mapping : tf.tensor (W, H)
            The mapping of the original input from which we drawn interpretable samples.
            Its size is equal to width and height of the current input
        num_features : int
            The dimension size of the interpretable space
        nb_samples : int
            The number of pertubed instances we want of current input
        ref_value : tf.tensor (C,)
            The reference value which replaces each feature when the corresponding
            interpretable feature is set to 0

        Returns
        -------
        interpret_samples : tf.tensor (nb_samples, num_features)
            Intrepretable samples of an input, with:
                nb_samples number of samples
                num_features the dimension of the interpretable space.
        pertubed_samples : tf.tensor (nb_samples, W, H, C)
            The samples corresponding to the masks applied to the original input
        """

        interpret_samples = pertub_func(num_features, nb_samples)

        masks = Lime._get_masks(interpret_samples, mapping)
        pertubed_samples = Lime._apply_masks(original_input, masks, ref_value)
        return interpret_samples, pertubed_samples

    @staticmethod
    def _get_exp_kernel_func(
        distance_mode: str = "euclidean", kernel_width: float = 1.0
    ):
        """
        This method allow to get the function which compute:
            exp(-D(original_input,pertubed_sample)^2/kernel_width^2)
        Where D is the distance defined by distance mode.

        Parameters
        ----------
        distance_mode : str
            Can be either euclidian or cosine
        kernel_width : float
            The size of the kernel

        Returns
        -------
        similarity_kernel: callable
            This callable should return a distance between an input and a pertubed sample
            (either in original space or in the interpretable space).

        """
        kernel_width = tf.cast(kernel_width,dtype=tf.float32)

        if distance_mode=="euclidean":
            @tf.function
            def _euclidean_similarity_kernel(original_input, sample, __):
                distance = None

                flatten_input = tf.reshape(original_input, [-1])
                flatten_sample = tf.reshape(sample, [-1])

                distance = tf.norm(flatten_input - flatten_sample, ord='euclidean')

                return tf.exp(-1.0 * (distance**2) / (kernel_width**2))

            return _euclidean_similarity_kernel

        if distance_mode=="cosine":
            @tf.function
            def _cosine_similarity_kernel(original_input, sample, __):
                distance = None

                flatten_input = tf.reshape(original_input, [-1])
                flatten_sample = tf.reshape(sample, [-1])

                distance = 1.0 - tf.keras.losses.cosine_similarity(flatten_input, flatten_sample)

                return tf.exp(-1.0 * (distance**2) / (kernel_width**2))

            return _cosine_similarity_kernel

        raise ValueError("distance_mode must be either cosine or euclidean.")

    @staticmethod
    @tf.function
    def _compute_similarities(curr_input, samples, interpret_samples, similarity_kernel):
        """
        This method call the similarity kernel nb_samples times to get the distances between an
        input sample and all the pertubed samples (either in the original input space or in the
        interpretable space)

        Parameters
        ----------
        curr_input : tf.tensor (W, H, C)
            The input we are explaining
        samples : tf.tensor (nb_samples, W, H, C)
            The pertubed samples of the current input
        interpret_samples : tf.tensor (nb_samples, num_features)
            The interpretable pertubed samples of the current input
        similarity_kernel : callable
            The function which allows to compute the distances between current input and its
            pertubed versions

        Returns
        -------
        similarities : tf.tensor (nb_samples)
            The similarities between current input and its pertubed versions
        """

        curr_inputs = tf.expand_dims(curr_input, axis=0)
        curr_inputs = tf.repeat(curr_inputs, repeats=len(samples), axis=0)

        similarities = tf.map_fn(fn= lambda inp: similarity_kernel(inp[0],inp[1],inp[2]),
                                 elems=(curr_inputs,samples,interpret_samples),
                                 fn_output_signature=tf.float32)
        return similarities

    @staticmethod
    @tf.function
    def _broadcast_explanation(explanation, mapping):
        """
        This method allows to broadcast explanations from the interpretable space to the
        corresponding super pixels

        Parameters
        ----------
        explanation : tf.tensor (num_features)
            Explanation value for each super pixel
        mapping : tf.tensor (W, H)
            The mapping of the original input from which we drawn interpretable samples
            (i.e super-pixels positions).
            Its size is equal to width and height of the original input

        Returns
        -------
        broadcast_explanation : tf.tensor (W, H)
            The explanation of the current input considered

        """
        broadcast_explanation = tf.gather(explanation, indices=mapping)
        return broadcast_explanation
