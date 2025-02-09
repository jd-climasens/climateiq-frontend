import main
import io

from contextlib import redirect_stdout
from unittest import mock
from cloudevents import http
from google.cloud import storage, pubsub_v1


def _create_pubsub_event() -> http.CloudEvent:
    attributes = {
        "type": "google.cloud.storage.object.v1.finalized",
        "source": "source",
    }
    data = {
        "bucket": "climateiq-predictions",
        "name": "id1/flood/v1.0/manhattan/extreme/prediction.results-3-of-5",
    }
    return http.CloudEvent(attributes, data)


def test_trigger_export_pipeline_invalid_object_name():
    attributes = {
        "type": "google.cloud.storage.object.v1.finalized",
        "source": "source",
    }
    data = {
        "bucket": "climateiq-predictions",
        "name": "invalid_name",  # Invalid object name
    }
    event = http.CloudEvent(attributes, data)

    output = io.StringIO()
    with redirect_stdout(output):
        main.trigger_export_pipeline(event)

    expected_error = (
        "Invalid object name format. Expected format: '<id>/<prediction_type>/"
        "<model_id>/<study_area_name>/<scenario_id>/prediction.results-"
        "<file_number>-of-{number_of_files_generated}'\nActual name: 'invalid_name'"
    )
    assert expected_error in output.getvalue()


@mock.patch.object(pubsub_v1, "PublisherClient", autospec=True)
@mock.patch.object(storage, "Client", autospec=True)
def test_trigger_export_pipeline_missing_prediction_files(
    mock_storage_client, mock_publisher
):
    event = _create_pubsub_event()

    # Missing predictions for chunks 2 and 4.
    input_blobs = [
        storage.Blob(
            name="id1/flood/v1.0/manhattan/extreme/prediction.results-1-of-5",
            bucket=storage.Bucket(mock_storage_client, "climateiq-predictions"),
        ),
        storage.Blob(
            name="id1/flood/v1.0/manhattan/extreme/prediction.results-3-of-5",
            bucket=storage.Bucket(mock_storage_client, "climateiq-predictions"),
        ),
        storage.Blob(
            name="id1/flood/v1.0/manhattan/extreme/prediction.results-5-of-5",
            bucket=storage.Bucket(mock_storage_client, "climateiq-predictions"),
        ),
    ]
    mock_storage_client().list_blobs.return_value = input_blobs

    main.trigger_export_pipeline(event)

    mock_publisher().topic_path.assert_not_called()


@mock.patch.object(pubsub_v1, "PublisherClient", autospec=True)
@mock.patch.object(storage, "Client", autospec=True)
def test_trigger_export_pipeline(mock_storage_client, mock_publisher):
    event = _create_pubsub_event()

    # Input blobs setup
    def create_mock_input_blob(name, start_chunk_id):
        chunk_id = (start_chunk_id - 1) * 2 + 1
        predictions = "\n".join(
            [
                f'{{"instance": {{"values": [{i}], "key": {chunk_id + i}}},'
                f'"prediction": [[1, 2, 3], [4, 5, 6]]}}'
                for i in range(2)
            ]
        )
        mock_blob = mock.MagicMock(spec=storage.Blob)
        mock_blob.name = name
        mock_file = mock.MagicMock()
        mock_file.__enter__.return_value = predictions.splitlines()
        mock_blob.open.return_value = mock_file
        return mock_blob

    input_blobs = [
        create_mock_input_blob(
            f"id1/flood/v1.0/manhattan/extreme/prediction.results-{i}-of-5", i
        )
        for i in range(1, 6)
    ]
    mock_storage_client().list_blobs.return_value = input_blobs

    # Publisher setup
    mock_publisher().topic_path.return_value = (
        "projects/climateiq/topics/climateiq-spatialize-and-export-predictions"
    )
    mock_future = mock.MagicMock()
    mock_future.result.return_value = "message_id"
    mock_publisher().publish.return_value = mock_future

    # Output blobs setup
    mock_output_blobs = {}
    mock_storage_client().bucket("").blob.side_effect = (
        lambda name: mock_output_blobs.setdefault(name, mock.MagicMock())
    )

    main.trigger_export_pipeline(event)

    # Confirm output blobs written
    for i in range(1, 11):
        expected_blob_name = f"id1/flood/v1.0/manhattan/extreme/{i}"
        output_blob = mock_output_blobs[expected_blob_name]
        expected_data = (
            f'{{"instance": {{"values": [{(i - 1) % 2}], "key": {i}}},'
            f'"prediction": [[1, 2, 3], [4, 5, 6]]}}'
        )
        output_blob.upload_from_string.assert_called_with(expected_data)

    # Confirm messages published
    expected_topic_name = (
        "projects/climateiq/topics/climateiq-spatialize-and-export-predictions"
    )
    expected_origin = "climateiq_trigger_export_pipeline_cf"
    expected_calls = [
        message
        for i in range(1, 11)
        for message in (
            mock.call(
                expected_topic_name,
                data=f"id1/flood/v1.0/manhattan/extreme/{i}".encode(),
                origin=expected_origin,
            ),
            mock.call().result(),
        )
    ]
    mock_publisher().publish.assert_has_calls(expected_calls)
