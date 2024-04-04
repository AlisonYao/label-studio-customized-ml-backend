import os

from label_studio_converter import brush
from typing import List, Dict, Optional
from uuid import uuid4
from sam_predictor import SAMPredictor
from label_studio_ml.model import LabelStudioMLBase
from label_studio_ml.utils import DATA_UNDEFINED_NAME
from google.cloud import storage
from datetime import timedelta
import base64
import requests
import logging
logger = logging.getLogger(__name__)

SAM_CHOICE = os.environ.get("SAM_CHOICE", "MobileSAM")  # other option is just SAM
PREDICTOR = SAMPredictor(SAM_CHOICE)


class SamMLBackend(LabelStudioMLBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.google_app_creds_json = self._get_custom_metadata('GOOGLE_APPLICATION_CREDENTIALS_BASE64')
        
    def _get_custom_metadata(self, metadata_key):
        metadata_url = 'http://metadata.google.internal/computeMetadata/v1/instance/attributes/'
        headers = {'Metadata-Flavor': 'Google'}
        metadata_request_url = metadata_url + metadata_key
        response = requests.get(metadata_request_url, headers=headers)
        assert response.status_code == 200
        google_app_creds_base64 = response.text
        google_app_creds_json = base64.b64decode(google_app_creds_base64).decode('utf-8')
        return google_app_creds_json
    
    def _get_image_url(self, task, value):
        image_url = task['data'].get(value) or task['data'].get(DATA_UNDEFINED_NAME)

        if image_url.startswith('gs://'):
            # Generate signed URL for GCS
            bucket_name, object_name = image_url.replace('gs://', '').split('/', 1)
            storage_client = storage.Client.from_service_account_info(self.google_app_creds_json)
            print("FREAKING service account key works!!!!!!!!!!!!!!")
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(object_name)
            try:
                image_url = blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(hours=1),  # Adjust expiration time as needed
                    method="GET",
                )
            except Exception as exc:
                logger.warning(f'Can\'t generate signed URL for {image_url}. Reason: {exc}')
        return image_url

    def predict(self, tasks: List[Dict], context: Optional[Dict] = None, **kwargs) -> List[Dict]:
        """ Returns the predicted mask for a smart keypoint that has been placed."""

        from_name, to_name, value = self.get_first_tag_occurence('BrushLabels', 'Image')

        if not context or not context.get('result'):
            # if there is no context, no interaction has happened yet
            return []

        image_width = context['result'][0]['original_width']
        image_height = context['result'][0]['original_height']

        # collect context information
        point_coords = []
        point_labels = []
        input_box = None
        selected_label = None
        for ctx in context['result']:
            x = ctx['value']['x'] * image_width / 100
            y = ctx['value']['y'] * image_height / 100
            ctx_type = ctx['type']
            selected_label = ctx['value'][ctx_type][0]
            if ctx_type == 'keypointlabels':
                point_labels.append(int(ctx.get('is_positive', 0)))
                point_coords.append([int(x), int(y)])
            elif ctx_type == 'rectanglelabels':
                box_width = ctx['value']['width'] * image_width / 100
                box_height = ctx['value']['height'] * image_height / 100
                input_box = [int(x), int(y), int(box_width + x), int(box_height + y)]

        print(f'Point coords are {point_coords}, point labels are {point_labels}, input box is {input_box}')

        img_path = tasks[0]['data'][value]
        task = tasks[0]
        img_path = self._get_image_url(task, value)
        predictor_results = PREDICTOR.predict(
            img_path=img_path,
            point_coords=point_coords or None,
            point_labels=point_labels or None,
            input_box=input_box
        )

        predictions = self.get_results(
            masks=predictor_results['masks'],
            probs=predictor_results['probs'],
            width=image_width,
            height=image_height,
            from_name=from_name,
            to_name=to_name,
            label=selected_label)

        return predictions

    def get_results(self, masks, probs, width, height, from_name, to_name, label):
        results = []
        for mask, prob in zip(masks, probs):
            # creates a random ID for your label everytime so no chance for errors
            label_id = str(uuid4())[:4]
            # converting the mask from the model to RLE format which is usable in Label Studio
            mask = mask * 255
            rle = brush.mask2rle(mask)

            results.append({
                'id': label_id,
                'from_name': from_name,
                'to_name': to_name,
                'original_width': width,
                'original_height': height,
                'image_rotation': 0,
                'value': {
                    'format': 'rle',
                    'rle': rle,
                    'brushlabels': [label],
                },
                'score': prob,
                'type': 'brushlabels',
                'readonly': False
            })

        return [{
            'result': results,
            'model_version': PREDICTOR.model_name
        }]


if __name__ == '__main__':
    # test the model
    model = SamMLBackend()
    model.use_label_config('''
    <View>
        <Image name="image" value="$image" zoom="true"/>
        <BrushLabels name="tag" toName="image">
            <Label value="Banana" background="#FF0000"/>
            <Label value="Orange" background="#0d14d3"/>
        </BrushLabels>
        <KeyPointLabels name="tag2" toName="image" smart="true" >
            <Label value="Banana" background="#000000" showInline="true"/>
            <Label value="Orange" background="#000000" showInline="true"/>
        </KeyPointLabels>
        <RectangleLabels name="tag3" toName="image"  >
            <Label value="Banana" background="#000000" showInline="true"/>
            <Label value="Orange" background="#000000" showInline="true"/>
        </RectangleLabels>
    </View>
    ''')
    results = model.predict(
        tasks=[{
            'data': {
                'image': 'https://s3.amazonaws.com/htx-pub/datasets/images/125245483_152578129892066_7843809718842085333_n.jpg'
            }}],
        context={
            'result': [{
                'original_width': 1080,
                'original_height': 1080,
                'image_rotation': 0,
                'value': {
                    'x': 49.441786283891545,
                    'y': 59.96810207336522,
                    'width': 0.3189792663476874,
                    'labels': ['Banana'],
                    'keypointlabels': ['Banana']
                },
                'is_positive': True,
                'id': 'fBWv1t0S2L',
                'from_name': 'tag2',
                'to_name': 'image',
                'type': 'keypointlabels',
                'origin': 'manual'
            }]}
    )
    import json
    results[0]['result'][0]['value']['rle'] = f'...{len(results[0]["result"][0]["value"]["rle"])} integers...'
    print(json.dumps(results, indent=2))
