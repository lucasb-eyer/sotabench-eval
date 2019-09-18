import os
import pickle
from pycocotools.coco import COCO
from sotabenchapi.check import in_check_mode
from sotabenchapi.client import Client
from sotabenchapi.core import BenchmarkResult, check_inputs

from sotabencheval.utils import calculate_batch_hash, download_url, change_root_if_server
from sotabencheval.object_detection.coco_eval import CocoEvaluator
from sotabencheval.object_detection.utils import get_coco_metrics


class COCOEvaluator(object):
    """`COCO <https://www.sotabench.com/benchmark/imagenet>`_ benchmark.

    Examples:
        Evaluate a ResNeXt model from the torchvision repository:

        .. code-block:: python

            import numpy as np
            import PIL
            import torch
            from sotabencheval.image_classification import ImageNetEvaluator
            from torchvision.models.resnet import resnext101_32x8d
            import torchvision.transforms as transforms
            from torchvision.datasets import ImageNet
            from torch.utils.data import DataLoader

            model = resnext101_32x8d(pretrained=True)

            # Define the transforms need to convert ImageNet data to expected
            # model input
            normalize = transforms.Normalize(
                mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
            )
            input_transform = transforms.Compose([
                transforms.Resize(256, PIL.Image.BICUBIC),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                normalize,
            ])

            test_dataset = ImageNet(
                './data',
                split="val",
                transform=input_transform,
                target_transform=None,
                download=True,
            )

            test_loader = DataLoader(
                test_dataset,
                batch_size=128,
                shuffle=False,
                num_workers=4,
                pin_memory=True,
            )

            model = model.cuda()
            model.eval()

            final_output = None
            evaluator = ImageNetEvaluator(
                             paper_model_name='ResNeXt-101-32x8d',
                             paper_arxiv_id='1611.05431')

            with torch.no_grad():
                for i, (input, target) in enumerate(test_loader):
                    input = input.to(device=device, non_blocking=True)
                    target = target.to(device=device, non_blocking=True)
                    output = model(input)

                    image_ids = [img[0].split('/')[-1].replace('.JPEG', '') for img in test_loader.dataset.imgs[i*test_loader.batch_size:(i+1)*test_loader.batch_size]]

                    evaluator.update(dict(zip(image_ids, list(output.cpu().numpy()))))

            print(evaluator.get_results())

            evaluator.save()
    """

    task = "Object Detection"

    def __init__(self,
                 root: str = '.',
                 split: str = "val",
                 dataset_year: str = "2017",
                 paper_model_name: str = None,
                 paper_arxiv_id: str = None,
                 paper_pwc_id: str = None,
                 paper_results: dict = None,
                 pytorch_hub_url: str = None,
                 model_description=None,):
        """Benchmarking function.

        Args:
            root (string): Root directory of the COCO Dataset.
            split (str) : the split for COCO to use, e.g. 'val'
            dataset_year (str): the dataset year for COCO to use; the
            paper_model_name (str, optional): The name of the model from the
                paper - if you want to link your build to a machine learning
                paper. See the COCO benchmark page for model names,
                https://www.sotabench.com/benchmark/coco, e.g. on the paper
                leaderboard tab.
            paper_arxiv_id (str, optional): Optional linking to ArXiv if you
                want to link to papers on the leaderboard; put in the
                corresponding paper's ArXiv ID, e.g. '1611.05431'.
            paper_pwc_id (str, optional): Optional linking to Papers With Code;
                put in the corresponding papers with code URL slug, e.g.
                'u-gat-it-unsupervised-generative-attentional'
            paper_results (dict, optional) : If the paper you are reproducing
                does not have model results on sotabench.com, you can specify
                the paper results yourself through this argument, where keys
                are metric names, values are metric values. e.g::

                    {'Top 1 Accuracy': 0.543, 'Top 5 Accuracy': 0.654}.

                Ensure that the metric names match those on the sotabench
                leaderboard - for COCO it should be 'box AP', 'AP50',
                'AP75', 'APS', 'APM', 'APL'
            pytorch_hub_url (str, optional): Optional linking to PyTorch Hub
                url if your model is linked there; e.g:
                'nvidia_deeplearningexamples_waveglow'.
            model_description (str, optional): Optional model description.
        """

        root = self.root = change_root_if_server(root=root,
                                                 server_root="./.data/vision/coco")

        self.paper_model_name = paper_model_name
        self.paper_arxiv_id = paper_arxiv_id
        self.paper_pwc_id = paper_pwc_id
        self.paper_results = paper_results
        self.pytorch_hub_url = pytorch_hub_url
        self.model_description = model_description

        annFile = os.path.join(
            root, "annotations/instances_%s%s.json" % (split, dataset_year)
        ),

        self.coco = COCO(annFile)
        self.iou_types = ['bbox']
        self.coco_evaluator = CocoEvaluator(self.coco, self.iou_types)

        self.detections = []
        self.results = None
        self.first_batch_processed = False
        self.batch_hash = None

    @property
    def cache_exists(self):
        """
        Checks whether the cache exists in the sotabench.com database - if so
        then sets self.results to cached results and returns True.

        You can use this property for control flow to break a for loop over a dataset
        after the first iteration. This prevents rerunning the same calculation for the
        same model twice.

        Examples:
            Breaking a for loop

            .. code-block:: python

                ...

                with torch.no_grad():
                    for i, (input, target) in enumerate(test_loader):
                        input = input.to(device=device, non_blocking=True)
                        target = target.to(device=device, non_blocking=True)
                        output = model(input)

                        image_ids = [img[0].split('/')[-1].replace('.JPEG', '') for img in test_loader.dataset.imgs[i*test_loader.batch_size:(i+1)*test_loader.batch_size]]

                        evaluator.update(dict(zip(image_ids, list(output.cpu().numpy()))))

                        if evaluator.cache_exists:
                            break

                evaluator.save()

        :return:
        """

        if not self.first_batch_processed:
            raise ValueError('No batches of data have been processed so no batch_hash exists')

        if not in_check_mode():
            return None

        client = Client.public()
        cached_res = client.get_results_by_run_hash(self.batch_hash)
        if cached_res:
            self.results = cached_res
            print(
                "No model change detected (using the first batch run "
                "hash). Will use cached results."
            )
            return True

        return False

    def update(self, detections: list):
        """
        Update the evaluator with new detections

        :param annotations (list): List of detections, that will be used by the COCO.loadRes method in the
        pycocotools API.  Each detection can take a dictionary format like the following:

        {'image_id': 397133, 'bbox': [386.1628112792969, 69.48855590820312, 110.14895629882812, 278.2847595214844],
        'score': 0.999152421951294, 'category_id': 1}

        I.e is a list of dictionaries.

        :return: void - updates self.detection with the new IDSs and prediction

        Examples:
            Update the evaluator with two results:

            .. code-block:: python

                my_evaluator.update([{'image_id': 397133, 'bbox': [386.1628112792969, 69.48855590820312,
                110.14895629882812, 278.2847595214844], 'score': 0.999152421951294, 'category_id': 1}])
        """

        self.detections.extend(detections)

        self.coco_evaluator.update(detections)

        if not self.first_batch_processed:
            self.batch_hash = calculate_batch_hash(detections)
            self.first_batch_processed = True

    def get_results(self):
        """
        Gets the results for the evaluator. This method only runs if predictions for all 5,000 ImageNet validation
        images are available. Otherwise raises an error and informs you of the missing or unmatched IDs.

        :return: dict with Top 1 and Top 5 Accuracy
        """

        annotation_image_ids = [ann['image_id'] for ann in self.coco.dataset['annotations']]
        ground_truth_image_ids = self.coco.getImgIds()

        if set(annotation_image_ids) != set(ground_truth_image_ids):
            missing_ids = set(ground_truth_image_ids) - set(annotation_image_ids)
            unmatched_ids = set(annotation_image_ids) - set(ground_truth_image_ids)

            if len(unmatched_ids) > 0:
                raise ValueError('''There are {mis_no} missing and {un_no} unmatched image IDs\n\n'''
                                     '''Missing IDs are {missing}\n\n'''
                                     '''Unmatched IDs are {unmatched}'''.format(mis_no=len(missing_ids),
                                                                                un_no=len(unmatched_ids),
                                                                                missing=missing_ids,
                                                                                unmatched=unmatched_ids))
            else:
                raise ValueError('''There are {mis_no} missing image IDs\n\n'''
                                     '''Missing IDs are {missing}'''.format(mis_no=len(missing_ids),
                                                                            missing=missing_ids))

        # Do the calculation only if we have all the results...
        self.coco_evaluator = CocoEvaluator(self.coco, self.iou_types)
        self.coco_evaluator.update(self.detections)
        self.coco_evaluator.evaluate()
        self.coco_evaluator.accumulate()
        self.coco_evaluator.summarize()

        self.results = get_coco_metrics(self.coco_evaluator)

        return self.results

    def save(self):
        """
        Calculate results and then put into a BenchmarkResult object

        On the sotabench.com server, this will produce a JSON file serialisation and results will be recorded
        on the platform.

        :return: BenchmarkResult object with results and metadata
        """

        # recalculate to ensure no mistakes made during batch-by-batch metric calculation
        self.get_results()

        return BenchmarkResult(
            task=self.task,
            config={},
            dataset='COCO minival',
            results=self.results,
            pytorch_hub_id=self.pytorch_hub_url,
            model=self.paper_model_name,
            model_description=self.model_description,
            arxiv_id=self.paper_arxiv_id,
            pwc_id=self.paper_pwc_id,
            paper_results=self.paper_results,
            run_hash=self.batch_hash,
        )