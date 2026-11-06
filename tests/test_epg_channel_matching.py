import json
import unittest
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from epg_utils import digit_signature, normalize_name
from merge_epg import (
    EpgSource, ProgrammeRow, SourceSpec, aliases_for, build_compatibility_ids,
    find_match, read_source_from_file, suggest_source_matches,
)

ROOT = Path(__file__).resolve().parents[1]


def build_source(channel_id: str, display_name: str, *, with_valid_schedule: bool = True,
                 raw_programmes: bool = True) -> EpgSource:
    spec = SourceSpec("test", "Fonte de teste", "local.xml", 1, "test", 1.0)
    ch = ET.Element("channel", {"id": channel_id})
    ET.SubElement(ch, "display-name").text = display_name
    channels = {channel_id: ch}
    names = {channel_id: [display_name]}
    norm = normalize_name(display_name)
    normalized_names = {channel_id: [norm]}
    programs = defaultdict(list)
    rows = defaultdict(list)
    exact = defaultdict(set)
    fuzzy = defaultdict(lambda: defaultdict(set))
    norm_id = normalize_name(channel_id)
    for cand in (display_name, channel_id):
        n = normalize_name(cand)
        if n:
            exact[n].add(channel_id)
            fuzzy[digit_signature(n)][n].add(channel_id)
    if raw_programmes:
        prog = ET.Element("programme", {"channel": channel_id, "start": "20261106080000 +0000", "stop": "20261106090000 +0000"})
        ET.SubElement(prog, "title").text = "Programa de teste"
        programs[channel_id].append(prog)
    if with_valid_schedule:
        start = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
        stop = start + timedelta(hours=1)
        row_element = ET.Element("programme", {"channel": channel_id})
        ET.SubElement(row_element, "title").text = "Programa de teste"
        rows[channel_id].append(ProgrammeRow(start, stop, "Programa de teste", "PROGRAMATESTE", row_element))
    return EpgSource(
        spec=spec, channels=channels, names=names, normalized_names=normalized_names,
        programs=dict(programs), rows=dict(rows), exact_index=dict(exact),
        fuzzy_index={sig: dict(items) for sig, items in fuzzy.items()},
        normalized_id_index={norm_id: {channel_id}},
    )


class ChannelMatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / "channels.json").read_text("utf-8"))
        cls.by_key = {entry["canonical_key"]: entry for entry in cls.catalog["channels"]}

    def test_alias_dictionary_unifies_dogtv_and_food_network(self):
        self.assertIn("DOGTV", self.by_key)
        self.assertIn("USDOGTV", [normalize_name(x) for x in self.by_key["DOGTV"]["aliases"]])
        self.assertIn("FOODNETWORK", self.by_key)
        self.assertIn("USFOODNETWORK", [normalize_name(x) for x in self.by_key["FOODNETWORK"]["aliases"]])
        self.assertIn("USFOODNETWORKEAST", self.by_key)

    def test_quality_variants_resolve_by_name(self):
        dog = self.by_key["DOGTV"]
        source = build_source("dogtv.br", "Dog TV")
        match = find_match(source, aliases_for(dog), dog.get("old_tvg_ids", []))
        self.assertIsNotNone(match)
        self.assertEqual(match.channel_id, "dogtv.br")

        food = self.by_key["FOODNETWORK"]
        source = build_source("foodnetwork.br", "Food Network HD")
        match = find_match(source, aliases_for(food), food.get("old_tvg_ids", []))
        self.assertIsNotNone(match)
        self.assertEqual(match.channel_id, "foodnetwork.br")

    def test_exact_name_without_valid_programmes_is_not_reported_as_match(self):
        dog = self.by_key["DOGTV"]
        source = build_source("dogtv.br", "Dog TV", with_valid_schedule=False, raw_programmes=True)
        self.assertIsNone(find_match(source, aliases_for(dog), []))
        suggestions = suggest_source_matches(source, aliases_for(dog))
        self.assertTrue(suggestions)
        self.assertEqual(suggestions[0]["valid_programmes"], 0)
        self.assertEqual(suggestions[0]["raw_programmes"], 1)

    def test_ambiguous_old_ids_are_never_emitted_as_compatibility_ids(self):
        entries = [
            {"id": "auto.cartoonito", "name": "Cartoonito", "old_tvg_ids": ["agromais.br"]},
            {"id": "auto.travelbox", "name": "Travel Box Brazil", "old_tvg_ids": ["agromais.br"]},
            {"id": "auto.sbttvjornalrecife", "name": "SBT TV Jornal Recife", "old_tvg_ids": ["agromais.br"]},
            {"id": "auto.multishow", "name": "Multishow", "old_tvg_ids": ["multishow.br"]},
        ]
        ids, ambiguous = build_compatibility_ids(entries)
        for value_list in ids.values():
            self.assertNotIn("agromais.br", value_list)
        self.assertIn("agromais.br", ambiguous["auto.cartoonito"])
        self.assertIn("multishow.br", ids["auto.multishow"])


    def test_programme_without_channel_declaration_is_indexed(self):
        dog = self.by_key["DOGTV"]
        spec = SourceSpec("test", "Fonte de teste", "local.xml", 1, "test", 1.0)
        xml = (b"<?xml version='1.0' encoding='utf-8'?>"
               b"<tv><programme channel='dogtv.br' start='20261106080000 +0000' stop='20261106090000 +0000'>"
               b"<title>Programa de teste</title></programme></tv>")
        with tempfile.NamedTemporaryFile(suffix=".xml") as f:
            f.write(xml); f.flush()
            source = read_source_from_file(spec, Path(f.name))
        match = find_match(source, aliases_for(dog), [])
        self.assertIsNotNone(match)
        self.assertEqual(match.channel_id, "dogtv.br")

    def test_off_variants_are_one_canonical_channel(self):
        self.assertIn("OFF", self.by_key)
        aliases = [normalize_name(x) for x in self.by_key["OFF"]["aliases"]]
        self.assertIn("CANALOFF", aliases)
        self.assertIn("OFF", aliases)

    def test_fashion_tv_multishow_curta_and_sbt_are_present(self):
        for key in ("FASHIONTV", "MULTISHOW", "CURTA", "SBT"):
            self.assertIn(key, self.by_key)

    def test_regional_sbt_is_not_merged_into_national_sbt(self):
        self.assertIn("SBT", self.by_key)
        self.assertIn("SBTTVJORNALRECIFE", self.by_key)
        self.assertNotEqual(self.by_key["SBT"]["id"], self.by_key["SBTTVJORNALRECIFE"]["id"])
        # sbt.br foi aprendido em canais nacionais e regionais; não pode ser
        # emitido como compatibilidade exclusiva do SBT nacional.
        self.assertNotIn("sbt.br", self.by_key["SBT"].get("old_tvg_ids", []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
