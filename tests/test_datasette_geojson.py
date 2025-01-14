import base64
import io
import geojson
import pytest

from pathlib import Path
from urllib.parse import urlencode
from datasette.app import Datasette
from geojson_to_sqlite.utils import import_features

from datasette_geojson import row_to_geojson

DATA = Path(__file__).parent / "data"
NEIGHBORHOODS = DATA / "Boston_Neighborhoods.geojson"
TABLE_NAME = "neighborhoods"


@pytest.fixture
def feature_collection():
    return geojson.loads(NEIGHBORHOODS.read_text())


@pytest.fixture
def spatial_database(tmp_path, feature_collection):
    db_path = tmp_path / "spatial.db"

    # specify pk to make lookups easier
    import_features(
        db_path, TABLE_NAME, feature_collection.features, spatialite=True, pk="OBJECTID"
    )
    return db_path


@pytest.fixture
def database(tmp_path, feature_collection):
    db_path = tmp_path / "test.db"
    import_features(db_path, TABLE_NAME, feature_collection.features)
    return db_path


@pytest.fixture
def image():
    path = DATA / "RoslindaleCC.JPG"
    return path.read_bytes()


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette([], memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-geojson" in installed_plugins


@pytest.mark.asyncio
async def test_render_feature_collection(database, feature_collection):
    datasette = Datasette([str(database)])

    # this will break with a path
    await datasette.refresh_schemas()

    # gut check
    results = await datasette.execute(
        database.stem, f"SELECT count(*) FROM {TABLE_NAME}"
    )
    count = results.first()[0]
    assert len(feature_collection["features"]) == count

    # build a url
    url = datasette.urls.table(database.stem, TABLE_NAME, format="geojson")

    response = await datasette.client.get(url)
    assert 200 == response.status_code

    fc = response.json()
    assert fc["type"] == "FeatureCollection"
    assert len(feature_collection["features"]) == len(fc["features"])

    for feature, expected in zip(fc["features"], feature_collection["features"]):
        row_id = feature["properties"].pop("rowid")  # sqlite adds this
        assert feature["properties"] == expected["properties"]
        assert feature["geometry"] == expected["geometry"]


@pytest.mark.asyncio
async def test_render_spatialite_table(spatial_database, feature_collection):
    datasette = Datasette([str(spatial_database)], sqlite_extensions=["spatialite"])

    SQL = f"SELECT Name, AsGeoJSON(geometry) as geometry FROM {TABLE_NAME}"

    # query url
    url = (
        datasette.urls.database(spatial_database.stem, format="geojson")
        + "?"
        + urlencode({"sql": SQL})
    )

    response = await datasette.client.get(url)
    fc = response.json()

    assert 200 == response.status_code
    assert fc["type"] == "FeatureCollection"
    assert len(feature_collection["features"]) == len(fc["features"])

    for feature, expected in zip(fc["features"], feature_collection["features"]):
        assert feature["properties"]["Name"] == expected["properties"]["Name"]
        assert feature["geometry"] == expected["geometry"]


@pytest.mark.asyncio
async def test_render_spatialite_blob(spatial_database, feature_collection):
    datasette = Datasette([str(spatial_database)], sqlite_extensions=["spatialite"])

    SQL = f"SELECT Name, geometry FROM {TABLE_NAME}"

    # query url
    url = (
        datasette.urls.database(spatial_database.stem, format="geojson")
        + "?"
        + urlencode({"sql": SQL})
    )

    response = await datasette.client.get(url)
    assert 200 == response.status_code

    fc = response.json()
    assert fc["type"] == "FeatureCollection"
    assert len(feature_collection["features"]) == len(fc["features"])

    for feature, expected in zip(fc["features"], feature_collection["features"]):
        assert feature["properties"]["Name"] == expected["properties"]["Name"]
        assert feature["geometry"] == expected["geometry"]


@pytest.mark.asyncio
async def test_render_geojson_newlines(database, feature_collection):
    datasette = Datasette([str(database)])

    # build a url
    url = (
        datasette.urls.table(database.stem, TABLE_NAME, format="geojson")
        + "?"
        + urlencode({"_nl": True})
    )

    response = await datasette.client.get(url)
    assert 200 == response.status_code

    features = list(decode_json_newlines(io.StringIO(response.text)))

    assert len(features) == len(feature_collection.features)
    assert all(f.is_valid for f in features)

    for feature, expected in zip(features, feature_collection["features"]):
        row_id = feature["properties"].pop("rowid")  # sqlite adds this
        assert feature["properties"] == expected["properties"]
        assert feature["geometry"] == expected["geometry"]


@pytest.mark.asyncio
async def test_rows_to_geojson(database):
    datasette = Datasette([database], sqlite_extensions=["spatialite"])
    db = datasette.get_database("test")

    results = await db.execute(f"SELECT Name, geometry FROM {TABLE_NAME}")
    features = [await row_to_geojson(row, db) for row in results.rows]

    assert all(f.is_valid for f in features)


@pytest.mark.asyncio
async def test_rows_with_null(spatial_database):
    datasette = Datasette([spatial_database], sqlite_extensions=["spatialite"])
    db = datasette.get_database("spatial")

    # insert a null feature
    await db.execute_write(
        f"INSERT INTO {TABLE_NAME} (Name) VALUES ('Null village')",
        block=True,
    )

    results = await db.execute(f"SELECT Name, geometry FROM {TABLE_NAME}")

    features = [await row_to_geojson(row, db) for row in results.rows]
    null = next(f for f in features if f["properties"]["Name"] == "Null village")

    assert null["geometry"] == None
    assert all(f.is_valid for f in features)


@pytest.mark.asyncio
async def test_binary_data(spatial_database, image):
    datasette = Datasette([spatial_database], sqlite_extensions=["spatialite"])
    db = datasette.get_database("spatial")
    encodeded = base64.b64encode(image).decode("latin1")

    # add a blob column
    await db.execute_write(f"ALTER TABLE {TABLE_NAME} ADD COLUMN image BLOB")

    # insert image
    await db.execute_write(
        f"UPDATE {TABLE_NAME} SET image = ? WHERE Name = 'Roslindale'", [image]
    )

    url = datasette.urls.row(spatial_database.stem, TABLE_NAME, 27, format="geojson")

    response = await datasette.client.get(url)
    assert 200 == response.status_code

    fc = response.json()
    properties = fc["features"][0]["properties"]

    assert properties["image"] == {"$base64": True, "encoded": encodeded}


def decode_json_newlines(file):
    for line in file:
        yield geojson.loads(line.strip())
