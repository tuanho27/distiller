#
# Copyright (c) 2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from functools import partial
import xlsxwriter
import yaml
import os
from sys import float_info
from collections import OrderedDict
from contextlib import contextmanager
import torch
from torchnet.meter import AverageValueMeter
import logging
from math import sqrt
import distiller
msglogger = logging.getLogger()

__all__ = ['SummaryActivationStatsCollector', 'RecordsActivationStatsCollector',
           'QuantCalibrationStatsCollector', 'collector_context', 'collectors_context']


class ActivationStatsCollector(object):
    """Collect model activation statistics information.

    ActivationStatsCollector is the base class for classes that collect activations statistics.
    You may collect statistics on different phases of the optimization process (training, validation, test).

    Statistics data are accessible via .value() or by accessing individual modules.

    The current implementation has a few caveats:
    * It is slow - therefore it is advisable to use this only when needed.
    * It can't access the activations of torch.Functions, only torch.Modules.

    ActivationStatsCollector uses the forward hook of modules in order to access the
    feature-maps.  This is both slow and limits us to seeing only the outputs of torch.Modules.
    We can remove some of the slowness, by choosing to log only specific layers or use it only
    during validation or test.  By default, we only log torch.nn.ReLU activations.

    The layer names are mangled, because torch.Modules don't have names and we need to invent
    a unique name per layer.  To assign human-readable names, it is advisable to invoke the following
    before starting the statistics collection:

        distiller.utils.assign_layer_fq_names(model)
    """
    def __init__(self, model, stat_name, classes):
        """
        Args:
            model - the model we are monitoring.
            stat_name - name for the statistics being collected.
                You can access a module's activation statistics by referring to module.<stat_name>
                For example:
                    print(module.sparsity)
            classes - a list of class types for which we collect activation statistics.
                Passing an empty list or None will collect statistics for all class types.
        """
        super(ActivationStatsCollector, self).__init__()
        self.model = model
        self.stat_name = stat_name
        self.classes = classes
        self.fwd_hook_handles = []

    def value(self):
        """Return a dictionary containing {layer_name: statistic}"""
        activation_stats = OrderedDict()
        self.model.apply(partial(self._collect_activations_stats, activation_stats=activation_stats))
        return activation_stats

    def start(self):
        """Start collecting activation stats.

        This will iteratively register the modules' forward-hooks, so that the collector
        will be called from the forward traversal and get exposed to activation data.
        """
        assert len(self.fwd_hook_handles) == 0
        self.model.apply(self.start_module)

    def start_module(self, module):
        """Iteratively register to the forward-pass callback of all eligible modules.

        Eligible modules are currently filtered by their class type.
        """
        is_leaf_node = len(list(module.children())) == 0
        register_all_class_types = not self.classes
        if is_leaf_node and (register_all_class_types or (type(module) in self.classes)):
            self.fwd_hook_handles.append(module.register_forward_hook(self._activation_stats_cb))
            self._start_counter(module)

    def stop(self):
        """Stop collecting activation stats.

        This will iteratively unregister the modules' forward-hooks.
        """
        for handle in self.fwd_hook_handles:
            handle.remove()
        self.fwd_hook_handles = []

    def reset(self):
        """Reset the statsitics counters of this collector."""
        self.model.apply(self._reset_counter)
        return self

    def save(self, fname):
        raise NotImplementedError

    def _activation_stats_cb(self, module, input, output):
        """Handle new activations ('output' argument).

        This is invoked from the forward-pass callback of module 'module'.
        """
        raise NotImplementedError

    def _start_counter(self, module):
        """Start a specific statistic counter - this is subclass-specific code"""
        raise NotImplementedError

    def _reset_counter(self, module):
        """Reset a specific statistic counter - this is subclass-specific code"""
        raise NotImplementedError

    def _collect_activations_stats(self, module, activation_stats, name=''):
        """Handle new activations - this is subclass-specific code"""
        raise NotImplementedError


class SummaryActivationStatsCollector(ActivationStatsCollector):
    """This class collects activiations statistical summaries.

    This Collector computes the mean of some statistic of the activation.  It is rather
    light-weight and quicker than collecting a record per activation.
    The statistic function is configured in the constructor.
    """
    def __init__(self, model, stat_name, summary_fn, classes=[torch.nn.ReLU]):
        super(SummaryActivationStatsCollector, self).__init__(model, stat_name, classes)
        self.summary_fn = summary_fn

    def _activation_stats_cb(self, module, input, output):
        """Record the activation sparsity of 'module'

        This is a callback from the forward() of 'module'.
        """
        try:
            getattr(module, self.stat_name).add(self.summary_fn(output.data))
        except RuntimeError as e:
            if "The expanded size of the tensor" in e.args[0]:
                raise ValueError("ActivationStatsCollector: a module ({} - {}) was encountered twice during model.apply().\n"
                                 "This is an indication that your model is using the same module instance, "
                                 "in multiple nodes in the graph.  This usually occurs with ReLU modules: \n"
                                 "For example in TorchVision's ResNet model, self.relu = nn.ReLU(inplace=True) is "
                                 "instantiated once, but used multiple times.  This is not permissible when using "
                                 "instances of ActivationStatsCollector.".
                                 format(module.distiller_name, type(module)))
            else:
                msglogger.info("Exception in _activation_stats_cb: {} {}".format(module.distiller_name, type(module)))
                raise

    def _start_counter(self, module):
        if not hasattr(module, self.stat_name):
            setattr(module, self.stat_name, AverageValueMeter())
            # Assign a name to this summary
            if hasattr(module, 'distiller_name'):
                getattr(module, self.stat_name).name = '_'.join((self.stat_name, module.distiller_name))
            else:
                getattr(module, self.stat_name).name = '_'.join((self.stat_name,
                                                                 module.__class__.__name__,
                                                                 str(id(module))))

    def _reset_counter(self, module):
        if hasattr(module, self.stat_name):
            getattr(module, self.stat_name).reset()

    def _collect_activations_stats(self, module, activation_stats, name=''):
        if hasattr(module, self.stat_name):
            mean = getattr(module, self.stat_name).mean
            if isinstance(mean, torch.Tensor):
                mean = mean.tolist()
            activation_stats[getattr(module, self.stat_name).name] = mean

    def save(self, fname):
        """Save the records to an Excel workbook, with one worksheet per layer.
        """
        fname = ".".join([fname, 'xlsx'])
        try:
            os.remove(fname)
        except OSError:
            pass

        records_dict = self.value()
        with xlsxwriter.Workbook(fname) as workbook:
            worksheet = workbook.add_worksheet(self.stat_name)
            col_names = []
            for col, (module_name, module_summary_data) in enumerate(records_dict.items()):
                if not isinstance(module_summary_data, list):
                    module_summary_data = [module_summary_data]
                worksheet.write_column(1, col, module_summary_data)
                col_names.append(module_name)
            worksheet.write_row(0, 0, col_names)
        return fname


class RecordsActivationStatsCollector(ActivationStatsCollector):
    """This class collects activations statistical records.

    This Collector computes a hard-coded set of activations statistics and collects a
    record per activation.  The activation records of the entire model (only filtered modules),
    can be saved to an Excel workbook.

    For obvious reasons, this is slower than SummaryActivationStatsCollector.
    """
    def __init__(self, model, classes=[torch.nn.ReLU]):
        super(RecordsActivationStatsCollector, self).__init__(model, "statistics_records", classes)

    def _activation_stats_cb(self, module, input, output):
        """Record the activation sparsity of 'module'

        This is a callback from the forward() of 'module'.
        """
        def to_np(stats):
            if isinstance(stats, tuple):
                return stats[0].detach().cpu().numpy()
            else:
                return stats.detach().cpu().numpy()

        # We get a batch of activations, from which we collect statistics
        if not output.is_contiguous():
            output = output.contiguous()
        act = output.view(output.size(0), -1)
        batch_min_list = to_np(torch.min(act, dim=1)).tolist()
        batch_max_list = to_np(torch.max(act, dim=1)).tolist()
        batch_mean_list = to_np(torch.mean(act, dim=1)).tolist()
        # If activation contains only a single element, standard-deviation is meaningless (and std() returns NaN)
        # Return 0 instead
        if act.shape[0] == act.numel():
            batch_std_list = to_np(torch.zeros(act.shape[0])).tolist()
        else:
            batch_std_list = to_np(torch.std(act, dim=1)).tolist()
        batch_l2_list = to_np(torch.norm(act, p=2, dim=1)).tolist()

        module.statistics_records['min'].extend(batch_min_list)
        module.statistics_records['max'].extend(batch_max_list)
        module.statistics_records['mean'].extend(batch_mean_list)
        module.statistics_records['std'].extend(batch_std_list)
        module.statistics_records['l2'].extend(batch_l2_list)
        module.statistics_records['shape'] = distiller.size2str(output)

    @staticmethod
    def _create_records_dict():
        records = OrderedDict()
        for stat_name in ['min', 'max', 'mean', 'std', 'l2']:
            records[stat_name] = []
        records['shape'] = ''
        return records

    def save(self, fname):
        """Save the records to an Excel workbook, with one worksheet per layer.
        """
        fname = ".".join([fname, 'xlsx'])
        try:
            os.remove(fname)
        except OSError:
            pass

        records_dict = self.value()
        with xlsxwriter.Workbook(fname) as workbook:
            for module_name, module_act_records in records_dict.items():
                worksheet = workbook.add_worksheet(module_name)
                col_names = []
                for col, (col_name, col_data) in enumerate(module_act_records.items()):
                    if col_name == 'shape':
                        continue
                    worksheet.write_column(1, col, col_data)
                    col_names.append(col_name)
                worksheet.write_row(0, 0, col_names)
                worksheet.write(0, len(col_names)+2, module_act_records['shape'])
        return fname

    def _start_counter(self, module):
        if not hasattr(module, "statistics_records"):
            module.statistics_records = self._create_records_dict()

    def _reset_counter(self, module):
        if hasattr(module, "statistics_records"):
            module.statistics_records = self._create_records_dict()

    def _collect_activations_stats(self, module, activation_stats, name=''):
        if hasattr(module, "statistics_records"):
            activation_stats[module.distiller_name] = module.statistics_records


class _QuantStatsRecord(object):
    @staticmethod
    def create_records_dict():
        records = OrderedDict()
        records['min'] = float_info.max
        records['max'] = -float_info.max
        for stat_name in ['avg_min', 'avg_max', 'mean', 'std']:
            records[stat_name] = 0
        records['shape'] = ''
        return records

    def __init__(self):
        # We don't know the number of inputs at this stage so we defer records creation to the actual callback
        self.inputs = []
        self.output = self.create_records_dict()


class QuantCalibrationStatsCollector(ActivationStatsCollector):
    """
    This class tracks activations stats required for quantization, for each layer and for each input
    and output. The tracked stats are:
      * Absolute min / max
      * Average min / max (calculate min / max per sample and average those)
      * Overall mean
      * Overall standard-deviation
    Calculated stats are saved to a YAML file.

    If a certain layer operates in-place, that layer's input stats will be overwritten by its output stats.
    The collector can, optionally, check for such cases at runtime. In addition, a simple mechanism to disable inplace
    operations in the model can be used. See arguments details below.

    Args:
        model (torch.nn.Module): The model we are monitoring
        classes (list): List of class types for which we collect activation statistics. Passing an empty list or
          None will collect statistics for all class types.
        inplace_runtime_check (bool): If True will raise an error if an in-place operation is detected
        disable_inplace_attrs (bool): If True, will search all modules within the model for attributes controlling
          in-place operations and disable them.
        inplace_attr_names (iterable): If disable_inplace_attrs is enabled, this is the list of attribute name
          that will be searched for.

    TODO: Consider merging with RecordsActivationStatsCollector
    Current differences between the classes:
      * Track single value per-input/output-per-module for the entire run. Specifically, for standard deviation this
        cannot be done by tracking per-activation std followed by some post-processing
      * Track inputs in addition to outputs
      * Different serialization (yaml vs xlsx)
    """
    def __init__(self, model, classes=None, inplace_runtime_check=False,
                 disable_inplace_attrs=False, inplace_attr_names=('inplace',)):
        super(QuantCalibrationStatsCollector, self).__init__(model, "quant_stats", classes)
        self.batch_idx = 0
        self.inplace_runtime_check = inplace_runtime_check

        if disable_inplace_attrs:
            if not inplace_attr_names:
                raise ValueError('inplace_attr_names cannot by empty or None')
            for m in model.modules():
                for n in inplace_attr_names:
                    if hasattr(m, n):
                        setattr(m, n, False)

    def _activation_stats_cb(self, module, inputs, output):
        def update_mean(old_mean, new_val):
            return old_mean + (new_val - old_mean) / module.batch_idx

        def update_std(values, old_std, old_mean, new_mean):
            # See here:
            # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Welford's_Online_algorithm
            numel = values.numel() if isinstance(values, torch.Tensor) else values.size
            total_values_so_far = numel * (module.batch_idx - 1)
            M = (old_std ** 2) * (total_values_so_far - 1)
            mean_diffs = (values - old_mean) * (values - new_mean)
            M += mean_diffs.sum()
            return sqrt((M / (total_values_so_far + numel - 1)).item())

        def update_record(record, tensor):
            if not tensor.is_contiguous():
                tensor = tensor.contiguous()
            act = tensor.view(tensor.size(0), -1)
            min_per_sample = act.min(dim=1)[0]
            max_per_sample = act.max(dim=1)[0]
            record['min'] = min(record['min'], min_per_sample.min().item())
            record['max'] = max(record['max'], max_per_sample.max().item())
            try:
                record['avg_min'] = update_mean(record['avg_min'], min_per_sample.mean().item())
                record['avg_max'] = update_mean(record['avg_max'], max_per_sample.mean().item())
                new_mean = update_mean(record['mean'], act.mean().item())
                record['std'] = update_std(tensor, record['std'], record['mean'], new_mean)
            except RuntimeError:
                record['avg_min'] = update_mean(record['avg_min'], min_per_sample.cpu().numpy().mean().item(0))
                record['avg_max'] = update_mean(record['avg_max'], max_per_sample.cpu().numpy().mean().item(0))
                new_mean = update_mean(record['mean'], act.cpu().numpy().mean().item(0))
                record['std'] = update_std(tensor.cpu().numpy(), record['std'], record['mean'], new_mean)
            record['mean'] = new_mean

            if not record['shape']:
                record['shape'] = distiller.size2str(tensor)

        if self.inplace_runtime_check and any([id(input) == id(output) for input in inputs]):
            raise RuntimeError('Inplace operation detected, meaning inputs stats are overridden by output stats. '
                               'You can either disable this check or make sure no in-place operations occur. '
                               'See QuantCalibrationStatsCollector class documentation for more info.')

        module.batch_idx += 1

        if not module.quant_stats.inputs:
            # Delayed initialization of inputs records, because only now we know the # of inputs
            for i in range(len(inputs)):
                module.quant_stats.inputs.append(_QuantStatsRecord.create_records_dict())

        for idx, input in enumerate(inputs):
            update_record(module.quant_stats.inputs[idx], input)
        update_record(module.quant_stats.output, output)

    def _start_counter(self, module):
        # We don't know the number of inputs at this stage so we defer records creation to the actual callback
        module.quant_stats = _QuantStatsRecord()
        module.batch_idx = 0

    def _reset_counter(self, module):
        # We don't know the number of inputs at this stage so we defer records creation to the actual callback
        module.quant_stats = _QuantStatsRecord()
        module.batch_idx = 0

    def _collect_activations_stats(self, module, activation_stats, name=''):
        if distiller.utils.has_children(module):
            return
        if not hasattr(module, 'quant_stats'):
            return

        activation_stats[module.distiller_name] = OrderedDict()
        if module.quant_stats.inputs:
            activation_stats[module.distiller_name]['inputs'] = OrderedDict()
            for idx, sr in enumerate(module.quant_stats.inputs):
                activation_stats[module.distiller_name]['inputs'][idx] = sr
        activation_stats[module.distiller_name]['output'] = module.quant_stats.output

    def save(self, fname):
        def ordered_dict_representer(self, value):
            return self.represent_mapping('tag:yaml.org,2002:map', value.items())
        yaml.add_representer(OrderedDict, ordered_dict_representer)

        if not fname.endswith('.yaml'):
            fname = ".".join([fname, 'yaml'])
        try:
            os.remove(fname)
        except OSError:
            pass

        records_dict = self.value()
        with open(fname, 'w') as f:
            yaml.dump(records_dict, f, default_flow_style=False)

        return fname


@contextmanager
def collector_context(collector):
    """A context manager for an activation collector"""
    if collector is not None:
        collector.reset().start()
    yield collector
    if collector is not None:
        collector.stop()


@contextmanager
def collectors_context(collectors_dict):
    """A context manager for a dictionary of collectors"""
    if len(collectors_dict) == 0:
        yield collectors_dict
        return
    for collector in collectors_dict.values():
        collector.reset().start()
    yield collectors_dict
    for collector in collectors_dict.values():
        collector.stop()


class TrainingProgressCollector(object):
    def __init__(self, stats={}):
        super(TrainingProgressCollector, self).__init__()
        object.__setattr__(self, '_stats', stats)

    def __setattr__(self, name, value):
        stats = self.__dict__.get('_stats')
        stats[name] = value

    def __getattr__(self, name):
        if name in self.__dict__['_stats']:
            return self.__dict__['_stats'][name]
        raise AttributeError("'{}' object has no attribute '{}'".format(
            type(self).__name__, name))

    def value(self):
        return self._stats
