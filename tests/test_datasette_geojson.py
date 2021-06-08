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

SQL = f"SELECT Name, geometry FROM {TABLE_NAME}"


@pytest.fixture
def feature_collection():
    return geojson.loads(NEIGHBORHOODS.read_text())


@pytest.fixture
def spatial_database(tmp_path, feature_collection):
    db_path = tmp_path / "spatial.db"
    import_features(db_path, TABLE_NAME, feature_collection, spatialite=True)
    return db_path


@pytest.fixture
def database(tmp_path, feature_collection):
    db_path = tmp_path / "test.db"
    import_features(db_path, TABLE_NAME, feature_collection.features)
    return db_path


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
    fc = response.json()

    assert 200 == response.status_code

    assert fc["type"] == "FeatureCollection"
    assert len(feature_collection["features"]) == len(fc["features"])

    for feature, expected in zip(fc["features"], feature_collection["features"]):
        row_id = feature["properties"].pop("rowid")  # sqlite adds this
        assert feature["properties"] == expected["properties"]
        assert feature["geometry"] == expected["geometry"]


@pytest.mark.asyncio
async def test_rows_to_geojson(database, feature_collection):
    datasette = Datasette([database], sqlite_extensions=["spatialite"])
    db = datasette.get_database("test")

    results = await db.execute(f"SELECT Name, geometry FROM {TABLE_NAME}")
    features = list(map(row_to_geojson, results.rows))

    assert all(f.is_valid for f in features)
