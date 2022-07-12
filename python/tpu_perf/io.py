import numpy as np
from collections import Iterable
import lmdb
try:
    from scipy.ndimage import zoom
    from skimage.transform import resize
    import skimage.io
except:
    pass

from . import blob_pb2 as ufw_blob


## proto / datum / ndarray conversion
def blobproto_to_array(blob, return_diff=False):
    """
    Convert a blob proto to an array. In default, we will just return the data,
    unless return_diff is True, in which case we will return the diff.
    """
    # Read the data into an array
    if return_diff:
        data = np.array(blob.diff)
    else:
        data = np.array(blob.data)

    # Reshape the array
    if blob.HasField('num') or blob.HasField('channels') or blob.HasField(
            'height') or blob.HasField('width'):
        # Use legacy 4D shape
        return data.reshape(blob.num, blob.channels, blob.height, blob.width)
    else:
        return data.reshape(blob.shape.dim)


dtype_dict = {
    np.float32: ufw_blob.BlobProto.Dtype.FP32,
    np.float16: ufw_blob.BlobProto.Dtype.FP16,
    np.int8: ufw_blob.BlobProto.Dtype.INT8,
    np.uint8: ufw_blob.BlobProto.Dtype.UINT8,
    np.int16: ufw_blob.BlobProto.Dtype.INT16,
    np.uint16: ufw_blob.BlobProto.Dtype.UINT16,
    np.int32: ufw_blob.BlobProto.Dtype.INT32,
    np.uint32: ufw_blob.BlobProto.Dtype.UINT32,
}

ufw_dtype = {np.dtype(k): v for k, v in dtype_dict.items()}
np_dtype = {v: k for k, v in dtype_dict.items()}


def array_to_blobproto(arr):
    """Converts a N-dimensional array to blob proto. If diff is given, also
    convert the diff. You need to make sure that arr and diff have the same
    shape, and this function does not do sanity check.
    """
    blob = ufw_blob.BlobProto()
    blob.shape.dim.extend(arr.shape)
    blob.dtype = ufw_dtype[arr.dtype]
    if arr.dtype in (
            np.float32,
            np.float16,
            np.float64,
    ):
        blob.data.extend(arr.astype(np.float32).flat)
        return blob
    if arr.dtype in (
            np.uint32,
            np.int32,
            np.int8,
            np.uint8,
            np.int16,
            np.uint16,
    ):
        blob.int32_data.extend(arr.astype(np.int32).flat)
        return blob
    raise Exception("unsupported numpy dtype:{}".format(arr.dtype))


def arraylist_to_blobprotovector_str(arraylist):
    """Converts a list of arrays to a serialized blobprotovec, which could be
    then passed to a network for processing.
    """
    vec = ufw_blob.BlobProtoVector()
    vec.blobs.extend([array_to_blobproto(arr) for arr in arraylist])
    return vec.SerializeToString()


def blobprotovector_str_to_arraylist(str):
    """Converts a serialized blobprotovec to a list of arrays.
    """
    vec = ufw_blob.BlobProtoVector()
    vec.ParseFromString(str)
    return [blobproto_to_array(blob) for blob in vec.blobs]


def array_to_datum(arr, label=None):
    """
    Converts a arbitrary-dimensional and arbitrary-dtype array to datum.
    """
    datum = ufw_blob.Datum()
    datum.shape.dim.extend(arr.shape)
    if arr.dtype == np.float32:
        datum.float_data.extend(arr.flat)
    else:
        datum.data = arr.view(np.uint8).tostring()
    datum.dtype = ufw_dtype[arr.dtype]
    if label is not None:
        datum.label = label
    return datum


def datum_to_array(datum):
    """Converts a datum to an array. Note that the label is not returned,
    as one can easily get it by calling datum.label.
    """
    if datum.HasField('shape'):
        shape = datum.shape.dim
    else:
        shape = [datum.channels, datum.height, datum.width]
    if len(datum.data):
        return np.fromstring(datum.data, dtype=np.uint8).view(
            np_dtype[datum.dtype]).reshape(shape)
    else:
        return np.array(datum.float_data).astype(np.float32).reshape(shape)


def blob_to_array(Input):
    """Converts a blob to an array. Note that the data type is float32 and
    int32 only.
    """
    if isinstance(Input, str):
        blob = ufw_blob.BlobProto()
        with open(Input, 'rb') as fp:
            blob.ParseFromString(fp.read())
    else:
        blob = Input
    shape = blob.shape.dim

    def toArray(data, dtype):
        nonlocal shape
        lenData = len(data)
        if shape == [] and lenData == 1:
            return data
        if shape == [] and lenData > 1:
            raise Exception(
                "Expected a scalar, but got an array with length {}".format(
                    lenData))
        if lenData > 0:
            return np.array(data).astype(dtype).reshape(shape)

    if len(blob.data) > 0:
        return toArray(blob.data, np.float32)
    if len(blob.int32_data) > 0:
        return toArray(blob.int32_data, np.int32)


## Pre-processing


class Transformer:
    """
    Transform input for feeding into a Net.

    Note: this is mostly for illustrative purposes and it is likely better
    to define your own input preprocessing routine for your needs.

    Parameters
    ----------
    net : a Net for which the input should be prepared
    """
    def __init__(self, inputs):
        self.inputs = inputs
        self.transpose = {}
        self.channel_swap = {}
        self.raw_scale = {}
        self.mean = {}
        self.input_scale = {}

    def __check_input(self, in_):
        if in_ not in self.inputs:
            raise Exception('{} is not one of the net inputs: {}'.format(
                in_, self.inputs))

    def preprocess(self, in_, data):
        """
        Format input for Caffe:
        - convert to single
        - resize to input dimensions (preserving number of channels)
        - transpose dimensions to K x H x W
        - reorder channels (for instance color to BGR)
        - scale raw input (e.g. from [0, 1] to [0, 255] for ImageNet models)
        - subtract mean
        - scale feature

        Parameters
        ----------
        in_ : name of input blob to preprocess for
        data : (H' x W' x K) ndarray

        Returns
        -------
        ufw_in : (K x H x W) ndarray for input to a Net
        """
        self.__check_input(in_)
        ufw_in = data.astype(np.float32, copy=False)
        transpose = self.transpose.get(in_)
        channel_swap = self.channel_swap.get(in_)
        raw_scale = self.raw_scale.get(in_)
        mean = self.mean.get(in_)
        input_scale = self.input_scale.get(in_)
        in_dims = self.inputs[in_][2:]
        if ufw_in.shape[:2] != in_dims:
            ufw_in = resize_image(ufw_in, in_dims)
        if transpose is not None:
            ufw_in = ufw_in.transpose(transpose)
        if channel_swap is not None:
            ufw_in = ufw_in[channel_swap, :, :]
        if raw_scale is not None:
            ufw_in *= raw_scale
        if mean is not None:
            ufw_in -= mean
        if input_scale is not None:
            ufw_in *= input_scale
        return ufw_in

    def deprocess(self, in_, data):
        """
        Invert Caffe formatting; see preprocess().
        """
        self.__check_input(in_)
        decaf_in = data.copy().squeeze()
        transpose = self.transpose.get(in_)
        channel_swap = self.channel_swap.get(in_)
        raw_scale = self.raw_scale.get(in_)
        mean = self.mean.get(in_)
        input_scale = self.input_scale.get(in_)
        if input_scale is not None:
            decaf_in /= input_scale
        if mean is not None:
            decaf_in += mean
        if raw_scale is not None:
            decaf_in /= raw_scale
        if channel_swap is not None:
            decaf_in = decaf_in[np.argsort(channel_swap), :, :]
        if transpose is not None:
            decaf_in = decaf_in.transpose(np.argsort(transpose))
        return decaf_in

    def set_transpose(self, in_, order):
        """
        Set the order of dimensions, e.g. to convert OpenCV's HxWxC images
        into CxHxW.

        Parameters
        ----------
        in_ : which input to assign this dimension order
        order : the order to transpose the dimensions
            for example (2,0,1) changes HxWxC into CxHxW and (1,2,0) reverts
        """
        self.__check_input(in_)
        if len(order) != len(self.inputs[in_]) - 1:
            raise Exception('Transpose order needs to have the same number of '
                            'dimensions as the input.')
        self.transpose[in_] = order

    def set_channel_swap(self, in_, order):
        """
        Set the input channel order for e.g. RGB to BGR conversion
        as needed for the reference ImageNet model.
        N.B. this assumes the channels are the first dimension AFTER transpose.

        Parameters
        ----------
        in_ : which input to assign this channel order
        order : the order to take the channels.
            (2,1,0) maps RGB to BGR for example.
        """
        self.__check_input(in_)
        if len(order) != self.inputs[in_][1]:
            raise Exception('Channel swap needs to have the same number of '
                            'dimensions as the input channels.')
        self.channel_swap[in_] = order

    def set_raw_scale(self, in_, scale):
        """
        Set the scale of raw features s.t. the input blob = input * scale.
        While Python represents images in [0, 1], certain Caffe models
        like CaffeNet and AlexNet represent images in [0, 255] so the raw_scale
        of these models must be 255.

        Parameters
        ----------
        in_ : which input to assign this scale factor
        scale : scale coefficient
        """
        self.__check_input(in_)
        self.raw_scale[in_] = scale

    def set_mean(self, in_, mean):
        """
        Set the mean to subtract for centering the data.

        Parameters
        ----------
        in_ : which input to assign this mean.
        mean : mean ndarray (input dimensional or broadcastable)
        """
        self.__check_input(in_)
        ms = mean.shape
        if mean.ndim == 1:
            # broadcast channels
            if ms[0] != self.inputs[in_][1]:
                raise ValueError('Mean channels incompatible with input.')
            mean = mean[:, np.newaxis, np.newaxis]
        else:
            # elementwise mean
            if len(ms) == 2:
                ms = (1, ) + ms
            if len(ms) != 3:
                raise ValueError('Mean shape invalid')
            if ms != self.inputs[in_][1:]:
                in_shape = self.inputs[in_][1:]
                m_min, m_max = mean.min(), mean.max()
                normal_mean = (mean - m_min) / (m_max - m_min)
                mean = resize_image(normal_mean.transpose((1,2,0)),
                        in_shape[1:]).transpose((2,0,1)) * \
                        (m_max - m_min) + m_min
        self.mean[in_] = mean

    def set_input_scale(self, in_, scale):
        """
        Set the scale of preprocessed inputs s.t. the blob = blob * scale.
        N.B. input_scale is done AFTER mean subtraction and other preprocessing
        while raw_scale is done BEFORE.

        Parameters
        ----------
        in_ : which input to assign this scale factor
        scale : scale coefficient
        """
        self.__check_input(in_)
        self.input_scale[in_] = scale


## Image IO


def load_image(filename, color=True):
    """
    Load an image converting from grayscale or alpha as needed.

    Parameters
    ----------
    filename : string
    color : boolean
        flag for color format. True (default) loads as RGB while False
        loads as intensity (if image is already grayscale).

    Returns
    -------
    image : an image with type np.float32 in range [0, 1]
        of size (H x W x 3) in RGB or
        of size (H x W x 1) in grayscale.
    """
    img = skimage.img_as_float(skimage.io.imread(
        filename, as_grey=not color)).astype(np.float32)
    if img.ndim == 2:
        img = img[:, :, np.newaxis]
        if color:
            img = np.tile(img, (1, 1, 3))
    elif img.shape[2] == 4:
        img = img[:, :, :3]
    return img


def resize_image(im, new_dims, interp_order=1):
    """
    Resize an image array with interpolation.

    Parameters
    ----------
    im : (H x W x K) ndarray
    new_dims : (height, width) tuple of new dimensions.
    interp_order : interpolation order, default is linear.

    Returns
    -------
    im : resized ndarray with shape (new_dims[0], new_dims[1], K)
    """
    if im.shape[-1] == 1 or im.shape[-1] == 3:
        im_min, im_max = im.min(), im.max()
        if im_max > im_min:
            # skimage is fast but only understands {1,3} channel images
            # in [0, 1].
            im_std = (im - im_min) / (im_max - im_min)
            resized_std = resize(im_std,
                                 new_dims,
                                 order=interp_order,
                                 mode='constant')
            resized_im = resized_std * (im_max - im_min) + im_min
        else:
            # the image is a constant -- avoid divide by 0
            ret = np.empty((new_dims[0], new_dims[1], im.shape[-1]),
                           dtype=np.float32)
            ret.fill(im_min)
            return ret
    else:
        # ndimage interpolates anything but more slowly.
        scale = tuple(np.array(new_dims, dtype=float) / np.array(im.shape[:2]))
        resized_im = zoom(im, scale + (1, ), order=interp_order)
    return resized_im.astype(np.float32)


def oversample(images, crop_dims):
    """
    Crop images into the four corners, center, and their mirrored versions.

    Parameters
    ----------
    image : iterable of (H x W x K) ndarrays
    crop_dims : (height, width) tuple for the crops.

    Returns
    -------
    crops : (10*N x H x W x K) ndarray of crops for number of inputs N.
    """
    # Dimensions and center.
    im_shape = np.array(images[0].shape)
    crop_dims = np.array(crop_dims)
    im_center = im_shape[:2] / 2.0

    # Make crop coordinates
    h_indices = (0, im_shape[0] - crop_dims[0])
    w_indices = (0, im_shape[1] - crop_dims[1])
    crops_ix = np.empty((5, 4), dtype=int)
    curr = 0
    for i in h_indices:
        for j in w_indices:
            crops_ix[curr] = (i, j, i + crop_dims[0], j + crop_dims[1])
            curr += 1
    crops_ix[4] = np.tile(im_center, (1, 2)) + np.concatenate(
        [-crop_dims / 2.0, crop_dims / 2.0])
    crops_ix = np.tile(crops_ix, (2, 1))

    # Extract crops
    crops = np.empty(
        (10 * len(images), crop_dims[0], crop_dims[1], im_shape[-1]),
        dtype=np.float32)
    ix = 0
    for im in images:
        for crop in crops_ix:
            crops[ix] = im[crop[0]:crop[2], crop[1]:crop[3], :]
            ix += 1
        crops[ix - 5:ix] = crops[ix - 5:ix, :, ::-1, :]  # flip for mirrors
    return crops


class LMDB_Dataset(object):
    def __init__(self, path, queue_size=100, map_size=20e6):
        self.db = lmdb.open(path,
                            map_size,
                            create=True,
                            lock=False,
                            map_async=True,
                            max_dbs=0)
        self.txn = self.db.begin(write=True)
        self.DB_KEY_FORMAT = "{:0>10d}__{:1}"
        self.queue_size = queue_size
        self.index = 0
        self.value_list = []
        self.key_list = []

    def put(self, images, labels=None, keys=None):
        if isinstance(images, np.ndarray):
            images = [images]

        if isinstance(keys, str):
            keys = [keys]

        num = len(images)
        if keys is None:
            keys = [''] * num
        assert (num == len(keys))
        keys = [
            self.DB_KEY_FORMAT.format(self.index + i, k)
            for i, k in enumerate(keys)
        ]

        def put_datum(arr, labels):
            num = len(arr)
            if isinstance(labels, Iterable):
                labels = [labels[i] for i in range(num)]
            if labels is None:
                labels = [None] * num
            elif isinstance(labels, int):
                labels = [labels]
            return [
                array_to_datum(g, l).SerializeToString()
                for g, l in zip(arr, labels)
            ]

        self.key_list.extend(keys)
        self.value_list.extend(put_datum(images, labels))
        self.index += num
        if len(self.key_list) >= self.queue_size:
            self._put_batch()

    def _put_batch(self):
        if len(self.key_list) == 0:
            return
        for key, value in zip(self.key_list, self.value_list):
            if not self._put(key, value):
                self._put_batch()
        self.txn.commit()
        self.txn = self.db.begin(write=True)
        self.value_list = []
        self.key_list = []

    def _put(self, key, value):
        success = False
        try:
            if isinstance(key, str):
                key = key.encode()
            self.txn.put(key, value, append=True)
            success = True
        except lmdb.MapFullError:
            self.txn.abort()
            curr_limit = self.db.info()['map_size']
            new_limit = curr_limit * 2
            self.db.set_mapsize(new_limit)
            self.txn = self.db.begin(write=True)
        return success

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self._put_batch()
        self.db.close()

    def __del__(self):
        self.close()


def lmdb_data(dataset_org):
    db_raw = lmdb.open(dataset_org, readonly=True)

    with db_raw.begin() as txn:
        cursor = txn.cursor()
        for key, value in cursor:
            datum = ufw_blob.Datum()
            datum.ParseFromString(value)
            yield key, datum_to_array(datum)
    db_raw.close()
