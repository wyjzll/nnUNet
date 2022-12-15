import shutil
from multiprocessing import Pool

import numpy as np
import torch
import torch.distributed as dist
from batchgenerators.utilities.file_and_folder_operations import *
from nnunetv2.configuration import default_num_processes
from nnunetv2.evaluation.evaluate_predictions import compute_metrics_on_folder
from nnunetv2.inference.export_prediction import export_prediction_from_softmax
from nnunetv2.inference.sliding_window_prediction import predict_sliding_window_return_logits, compute_gaussian
from nnunetv2.network_architecture.hrnet.hrnet import MODEL_CONFIGS, get_seg_model
from nnunetv2.training.dataloading.nnunet_dataset import nnUNetDataset
from nnunetv2.training.dataloading.utils import unpack_dataset
from nnunetv2.training.nnUNetTrainer.variants.network_architecture.nnUNetTrainerNoDeepSupervision import \
    nnUNetTrainerNoDeepSupervision
from nnunetv2.utilities.default_n_proc_DA import get_allowed_n_proc_DA
from nnunetv2.utilities.file_path_utilities import should_i_save_to_file
from nnunetv2.utilities.label_handling.label_handling import convert_labelmap_to_one_hot
from nnunetv2.utilities.plans_handling.plans_handler import PlansManager, ConfigurationManager
from torch import nn


class nnUNetTrainer_HRNet18(nnUNetTrainerNoDeepSupervision):
    """
    only does 2d and does not adapt the network architecture. This is intended as a PoC to see if HRNet can do
    anything for us here
    """
    @staticmethod
    def build_network_architecture(plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True) -> nn.Module:
        label_manager = plans_manager.get_label_manager(dataset_json)
        return get_seg_model(MODEL_CONFIGS['hrnet18'],
                             label_manager.num_segmentation_heads,
                             input_channels=num_input_channels)

    def on_train_start(self):
        """
        remove references to encoder and decoder
        """
        if not self.was_initialized:
            self.initialize()

        maybe_mkdir_p(self.output_folder)

        self.print_plans()

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        # maybe unpack
        if self.unpack_dataset and (not self.is_ddp or self.local_rank == 0):
            self.print_to_log_file('unpacking dataset...')
            unpack_dataset(self.preprocessed_dataset_folder, unpack_segmentation=True, overwrite_existing=False,
                           num_processes=max(1, round(get_allowed_n_proc_DA() // 2)))
            self.print_to_log_file('unpacking done...')

        if self.is_ddp:
            dist.barrier()

        # dataloaders must be instantiated here because they need access to the training data which may not be present
        # when doing inference
        self.dataloader_train, self.dataloader_val = self.get_dataloaders()

        # copy plans and dataset.json so that they can be used for restoring everything we need for inference
        save_json(self.plans_manager.plans, join(self.output_folder_base, 'plans.json'))
        save_json(self.dataset_json, join(self.output_folder_base, 'dataset.json'))

        # we don't really need the fingerprint but its still handy to have it with the others
        shutil.copy(join(self.preprocessed_dataset_folder_base, 'dataset_fingerprint.json'),
                    join(self.output_folder_base, 'dataset_fingerprint.json'))

        # produces a pdf in output folder
        self.plot_network_architecture()

    def perform_actual_validation(self, save_probabilities: bool = False):
        """
        remove references to encoder and decoder
        """
        num_seg_heads = self.label_manager.num_segmentation_heads

        inference_gaussian = torch.from_numpy(
            compute_gaussian(self.configuration_manager.patch_size, sigma_scale=1. / 8))
        segmentation_export_pool = Pool(default_num_processes)
        validation_output_folder = join(self.output_folder, 'validation')
        maybe_mkdir_p(validation_output_folder)

        # we cannot use self.get_tr_and_val_datasets() here because we might be DDP and then we have to distribute
        # the validation keys across the workers.
        _, val_keys = self.do_split()
        if self.is_ddp:
            val_keys = val_keys[self.local_rank:: dist.get_world_size()]

        dataset_val = nnUNetDataset(self.preprocessed_dataset_folder, val_keys,
                                    folder_with_segs_from_previous_stage=self.folder_with_segs_from_previous_stage)

        next_stages = self.configuration_manager.next_stage_names

        if next_stages is not None:
            _ = [maybe_mkdir_p(join(self.output_folder_base, 'predicted_next_stage', n)) for n in next_stages]

        results = []
        for k in dataset_val.keys():
            self.print_to_log_file(f"predicting {k}")
            data, seg, properties = dataset_val.load_case(k)

            if self.is_cascaded:
                data = np.vstack((data, convert_labelmap_to_one_hot(seg[-1], self.label_manager.foreground_labels,
                                                                    output_dtype=data.dtype)))

            output_filename_truncated = join(validation_output_folder, k)

            prediction = predict_sliding_window_return_logits(self.network, data, num_seg_heads,
                                                              tile_size=self.configuration_manager.patch_size,
                                                              mirror_axes=self.inference_allowed_mirroring_axes,
                                                              tile_step_size=0.5,
                                                              use_gaussian=True,
                                                              precomputed_gaussian=inference_gaussian,
                                                              perform_everything_on_gpu=True,
                                                              verbose=False,
                                                              device=self.device).cpu().numpy()
            if should_i_save_to_file(prediction, results, segmentation_export_pool):
                np.save(output_filename_truncated + '.npy', prediction)
                prediction_for_export = output_filename_truncated + '.npy'
            else:
                prediction_for_export = prediction

            # this needs to go into background processes
            results.append(
                segmentation_export_pool.starmap_async(
                    export_prediction_from_softmax, (
                        (prediction_for_export, properties, self.configuration_manager, self.plans_manager,
                         self.dataset_json, output_filename_truncated, save_probabilities),
                    )
                )
            )
            # for debug purposes
            # export_prediction(prediction_for_export, properties, self.configuration, self.plans, self.dataset_json,
            #              output_filename_truncated, save_probabilities)

            # if needed, export the softmax prediction for the next stage
            if next_stages is not None:
                print('Cascade not supported here!')

        _ = [r.get() for r in results]

        segmentation_export_pool.close()
        segmentation_export_pool.join()

        if self.is_ddp:
            dist.barrier()

        if not self.is_ddp or self.local_rank == 0:
            compute_metrics_on_folder(join(self.preprocessed_dataset_folder_base, 'gt_segmentations'),
                                      validation_output_folder,
                                      join(validation_output_folder, 'summary.json'),
                                      self.plans_manager.image_reader_writer_class(),
                                      self.dataset_json["file_ending"],
                                      self.label_manager.foreground_regions if self.label_manager.has_regions else
                                      self.label_manager.foreground_labels,
                                      self.label_manager.ignore_label)


class nnUNetTrainer_HRNet32(nnUNetTrainer_HRNet18):
    """
    only does 2d and does not adapt the network architecture. This is intended as a PoC to see if HRNet can do
    anything for us here
    """
    @staticmethod
    def build_network_architecture(plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True) -> nn.Module:
        label_manager = plans_manager.get_label_manager(dataset_json)
        return get_seg_model(MODEL_CONFIGS['hrnet32'],
                             label_manager.num_segmentation_heads,
                             input_channels=num_input_channels)


class nnUNetTrainer_HRNet48(nnUNetTrainer_HRNet18):
    """
    only does 2d and does not adapt the network architecture. This is intended as a PoC to see if HRNet can do
    anything for us here
    """
    @staticmethod
    def build_network_architecture(plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True) -> nn.Module:
        label_manager = plans_manager.get_label_manager(dataset_json)
        return get_seg_model(MODEL_CONFIGS['hrnet48'],
                             label_manager.num_segmentation_heads,
                             input_channels=num_input_channels)