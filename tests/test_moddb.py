import asyncio

from app.services.moddb import ModDBClient, ModDBData, _pick_summary


def test_show_mod_number_is_asset_id_not_internal_mod_id():
    rows = [
        {"modid": 8288, "assetid": 47577, "name": "Ad Astra", "urlalias": "adastra"},
        {"modid": 11938, "assetid": 37634, "name": "Clothing Visually Degrades", "urlalias": "clothingvisuallydegrades"},
    ]
    row = _pick_summary(
        rows, "37634", None, None,
        source_url="https://mods.vintagestory.at/show/mod/37634",
    )
    assert row["name"] == "Clothing Visually Degrades"
    assert row["modid"] == 11938
    assert row["assetid"] == 37634


def test_alias_resolves_to_correct_catalog_row():
    rows = [
        {"modid": 8288, "assetid": 47577, "name": "Ad Astra", "urlalias": "adastra"},
        {"modid": 8987, "assetid": 51937, "name": "Immersive Quicklime", "urlalias": "immersivequicklime"},
    ]
    row = _pick_summary(
        rows, "adastra", "adastra", None,
        source_url="https://mods.vintagestory.at/adastra",
    )
    assert row["name"] == "Ad Astra"
    assert row["modid"] == 8288
    assert row["assetid"] == 47577


def test_page_metadata_overrides_wrong_api_metadata_and_never_uses_disclaimer_h1():
    data = ModDBData(
        mod_db_id=11938,
        asset_id=37634,
        mod_id="clothingvisuallydegrades",
        name="Wrong API name",
        description="Long description",
        short_description="Wrong API summary",
        image_url="https://moddbcdn.vintagestory.at/wrong.png",
        author=None,
        mod_type=None,
        mod_db_url="https://mods.vintagestory.at/show/mod/37634",
        releases=[],
        source_updated_at=None,
        raw_json="{}",
    )
    html = '''
    <html><head>
      <meta property="og:title" content="Clothing Visually Degrades | Vintage Story Mod DB">
      <meta property="og:description" content="Deteriorates clothing visually as it takes damage.">
      <meta property="og:image" content="https://moddbcdn.vintagestory.at/correct.png">
    </head><body>
      <h1>Disclaimer</h1>
      <main><h2>Clothing Visually Degrades</h2></main>
    </body></html>'''
    enriched = ModDBClient.enrich_from_page_html(data, html)
    assert enriched.name == "Clothing Visually Degrades"
    assert enriched.short_description == "Deteriorates clothing visually as it takes damage."
    assert enriched.image_url.endswith("/correct.png")
    assert enriched.asset_id == 37634


def test_page_release_table_identifier_is_used():
    data = ModDBData(
        mod_db_id=None, asset_id=15145, mod_id=None, name="15145", description=None,
        short_description=None, image_url=None, author=None, mod_type=None,
        mod_db_url="https://mods.vintagestory.at/show/mod/15145", releases=[],
        source_updated_at=None, raw_json="{}",
    )
    html = '''
    <html><head><meta property="og:title" content="Jack's Dryable Firewood"></head><body>
    <h1>Disclaimer</h1>
    <table><tr><th>Mod Version</th><th>Mod Identifier</th><th>For Game version</th><th>Download</th></tr>
    <tr><td>1.7.0</td><td>JacksFirewood</td><td>1.22.5</td><td>x.zip</td></tr>
    </table></body></html>'''
    enriched = ModDBClient.enrich_from_page_html(data, html)
    assert enriched.name == "Jack's Dryable Firewood"
    assert enriched.mod_id == "JacksFirewood"
    assert enriched.mod_id != "15145"


def test_get_mod_resolves_public_asset_to_internal_api_modid(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeClient:
        async def get(self, url, **kwargs):
            calls.append(url)
            return Response({
                "mod": {
                    "modid": 8288,
                    "assetid": 47577,
                    "name": "Ad Astra",
                    "modidstr": "adastra",
                    "releases": [{"releaseid": 1, "modidstr": "adastra", "modversion": "1.0.0", "tags": ["1.22.7"]}],
                }
            })

    client = ModDBClient("https://mods.vintagestory.at/api", "https://mods.vintagestory.at")
    monkeypatch.setattr(client, "_get_client", lambda: asyncio.sleep(0, result=FakeClient()))
    result = asyncio.run(client.get_mod(
        "47577",
        source_url="https://mods.vintagestory.at/show/mod/47577",
        catalog_rows=[{"modid": 8288, "assetid": 47577, "name": "Ad Astra", "urlalias": "adastra"}],
    ))
    assert result.mod_db_id == 8288
    assert result.asset_id == 47577
    assert result.mod_id == "adastra"
    assert result.mod_db_url.endswith("/show/mod/47577")
    assert calls == ["https://mods.vintagestory.at/api/mod/8288"]


def test_show_mod_asset_is_never_used_directly_as_api_identifier_when_catalog_mapping_missing(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"mod": {"modid": 9999, "assetid": 1234, "name": "WRONG"}}

    class FakeClient:
        async def get(self, url, **kwargs):
            calls.append(url)
            return Response()

    client = ModDBClient("https://mods.vintagestory.at/api", "https://mods.vintagestory.at")
    monkeypatch.setattr(client, "_get_client", lambda: asyncio.sleep(0, result=FakeClient()))
    result = asyncio.run(client.get_mod(
        "1234",
        source_url="https://mods.vintagestory.at/show/mod/1234",
        catalog_rows=[],
    ))
    assert result.asset_id == 1234
    assert calls == []


def test_page_only_fallback_does_not_call_show_mod_number_as_api_id(monkeypatch):
    import asyncio

    class Response:
        def raise_for_status(self):
            raise RuntimeError("not found")
        def json(self):
            return {}

    class FakeClient:
        async def get(self, url, **kwargs):
            raise AssertionError(f"unexpected API request: {url}")

    client = ModDBClient("https://mods.vintagestory.at/api", "https://mods.vintagestory.at")
    monkeypatch.setattr(client, "_get_client", lambda: asyncio.sleep(0, result=FakeClient()))
    result = asyncio.run(client.get_mod("64026", source_url="https://mods.vintagestory.at/show/mod/64026", catalog_rows=[]))
    assert result.asset_id == 64026
    assert result.mod_db_url.endswith("/show/mod/64026")
