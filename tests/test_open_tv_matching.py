"""Regressão: afiliadas abertas (Globo/SBT/Record/Band) não podem herdar grade de outra emissora/UF."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import merge_epg as m  # noqa: E402
from merge_epg import name_collisions as collisions, dedupe_display_names, name_key  # noqa: E402


def tiers(nome):
    return m.expand_open_tv_tiers(m.normalizar_canal(nome), "")


def flat(nome):
    return [a for t in tiers(nome) for a in t]


class OpenTvTiers(unittest.TestCase):
    def test_sbt_recife_nao_recebe_aliases_da_globo(self):
        self.assertFalse([a for a in flat("SBT TV JORNAL RECIFE") if "GLOBO" in a])
        self.assertIn("SBT PE", flat("SBT TV JORNAL RECIFE"))

    def test_record_belem_nao_recebe_tv_liberal(self):
        self.assertFalse([a for a in flat("RECORD TV BELEM") if "LIBERAL" in a or a.startswith("GLOBO")])

    def test_globo_asa_branca_mantem_aliases_locais(self):
        t1, t2, _ = tiers("GLOBO ASA BRANCA")
        self.assertIn("TV ASA BRANCA", t1)
        self.assertIn("GLOBO PE", t2)

    def test_band_continua_com_fallback_proprio(self):
        self.assertIn("BAND SP", tiers("BAND TV SP")[2])


class FuzzyGuard(unittest.TestCase):
    def test_bloqueia_uf_diferente(self):
        self.assertTrue(m._fuzzy_incompatible("GLOBOPE", "GLOBOSP"))
        self.assertTrue(m._fuzzy_incompatible("GLOBOPA", "GLOBOSP"))

    def test_bloqueia_emissora_diferente(self):
        self.assertTrue(m._fuzzy_incompatible("SBTRECIFE", "GLOBORECIFE"))

    def test_permite_variacao_do_mesmo_canal(self):
        self.assertFalse(m._fuzzy_incompatible("GLOBOPE", "GLOBOPERNAMBUCO"))


class NameDedupe(unittest.TestCase):
    def test_cada_nome_tem_um_dono(self):
        names = {
            "Globo São Paulo": ["Globo São Paulo", "Globo EPTV Campinas HD", "Globo EPTV Campinas FHD"],
            "Globo EPTV Campinas": ["Globo EPTV Campinas", "Globo EPTV Campinas HD"],
        }
        out = dedupe_display_names(names)
        self.assertFalse(collisions(out))
        self.assertIn("Globo EPTV Campinas HD", out["Globo EPTV Campinas"])
        self.assertNotIn("Globo EPTV Campinas HD", out["Globo São Paulo"])

    def test_chave_preserva_parenteses(self):
        self.assertEqual(name_key("GLOBO PE (ASA BRANCA) HD"), "GLOBO PE ASA BRANCA")


class IdsEnvenenados(unittest.TestCase):
    def test_set_manual_nao_foi_sobrescrito(self):
        self.assertIn("aande", m.POISONED_OPEN_TV_IDS)
        self.assertIn("globosp", m.POISONED_OPEN_TV_IDS)

    def test_aande_usado_por_canais_diferentes_e_detectado(self):
        pares = [("AandE.br", "TV Brasil HD [3]"), ("AandE.br", "Cartoonito HD [1]"),
                 ("AandE.br", "Tooncast SD [1]"), ("AandE.br", "Record News HD [3]")]
        self.assertIn("aande", m.detect_poisoned_ids(pares))

    def test_variantes_de_qualidade_nao_envenenam(self):
        pares = [("Cartoonito.br", "CARTOONITO SD"), ("Cartoonito.br", "CARTOONITO HD"),
                 ("Cartoonito.br", "CARTOONITO FHD [H265]")]
        self.assertFalse(m.detect_poisoned_ids(pares))

    def test_id_envenenado_nao_e_exportado(self):
        m.AUTO_POISONED_IDS.add("aande")
        try:
            self.assertFalse(m.is_valid_provider_tvid("A&E", "aande.br"))
        finally:
            m.AUTO_POISONED_IDS.discard("aande")


class CartoonitoRegressao(unittest.TestCase):
    def test_cartoonito_so_no_proprio_canal(self):
        out = dedupe_display_names({
            "Cartoonito": ["Cartoonito", "Cartoonito HD [1]"],
            "TV BRASIL": ["TV BRASIL", "Cartoonito HD [1] HD ²", "Cartoonito HD [1]"],
        })
        self.assertFalse(collisions(out))
        self.assertFalse([n for n in out["TV BRASIL"] if "Cartoonito" in n])


class BrasiliaECampinas(unittest.TestCase):
    class _Src:
        names = {}

    def test_brasilia_nao_vira_tv_brasil(self):
        self.assertTrue(m._fuzzy_incompatible("TVBRASILIA", "TVBRASIL"))
        self.assertTrue(m._fuzzy_incompatible("RECORDTVBRASILIA", "RECORDTVBRASIL"))

    def test_fuzzy_nao_inventa_marca_da_globo(self):
        self.assertTrue(m._fuzzy_incompatible("TVCAMPINAS", "EPTVCAMPINAS"))
        self.assertFalse(m._fuzzy_incompatible("EPCAMPINAS", "EPTVCAMPINAS"))  # abreviação legítima

    def test_mesma_emissora_cidade_diferente(self):
        self.assertTrue(m._fuzzy_incompatible("SBTCAMPINAS", "SBTMINAS"))
        self.assertTrue(m._fuzzy_incompatible("RECORDTVMANAUS", "RECORDTVMINAS"))
        self.assertFalse(m._fuzzy_incompatible("GLOBOTVVANGSJCAMPOS", "GLOBOTVVANGUARDASJDOSCAMPOS"))

    def test_sbt_record_band_nao_recebem_outra_emissora(self):
        c = lambda nome, cid: m.match_conflicts_network(nome, self._Src, cid)
        self.assertTrue(c("BAND CAMPINAS", "EPTVCampinas.br"))
        self.assertTrue(c("RECORD BRASILIA", "TV Brasil"))
        self.assertTrue(c("SBT SP", "Rede TV"))
        self.assertTrue(c("BAND TV GOIANIA", "sbt-goiania-tv-serra-dourada"))
        self.assertTrue(c("RECORDTV FLORIANOPOLIS", "nsc-tv-florianopolis-hd"))
        self.assertFalse(c("SBT TV ALTEROSA", "TV Alterosa HD"))
        self.assertFalse(c("BAND BRASILIA", "Band Brasília HD"))
        self.assertFalse(c("GLOBO EPTV CAMPINAS", "EPTVCampinas.br"))  # Globo pode usar EPTV


# ---------------------------------------------------------------- fusão de fontes
import gzip
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _xmltv(channels: dict, programmes: dict) -> Path:
    root = ET.Element("tv")
    for cid, dn in channels.items():
        ch = ET.SubElement(root, "channel", id=cid)
        ET.SubElement(ch, "display-name").text = dn
    for cid, rows in programmes.items():
        for start, stop, title in rows:
            pr = ET.SubElement(root, "programme", start=start.strftime("%Y%m%d%H%M%S +0000"),
                               stop=stop.strftime("%Y%m%d%H%M%S +0000"), channel=cid)
            ET.SubElement(pr, "title").text = title
    f = tempfile.NamedTemporaryFile(suffix=".xml.gz", delete=False)
    f.write(gzip.compress(ET.tostring(root))); f.close()
    return Path(f.name)


def _spec(key="s1"):
    return m.SourceSpec(key=key, label=key, url="", priority=1, family="f", vote_weight=1.0)


T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


class PlaceholdersEFusao(unittest.TestCase):
    def test_no_data_nao_e_programa(self):
        self.assertFalse(m.is_meaningful_programme(m.normalize_programme_title("No Data")))
        self.assertFalse(m.is_meaningful_programme(m.normalize_programme_title("NO DATA")))
        self.assertTrue(m.is_meaningful_programme(m.normalize_programme_title("Jornal da Band")))

    def test_canal_so_com_no_data_fica_sem_grade(self):
        rows = [(T0 + timedelta(minutes=20 * i), T0 + timedelta(minutes=20 * (i + 1)), "No Data") for i in range(73)]
        real = [(T0 + timedelta(hours=i), T0 + timedelta(hours=i + 1), f"Programa {i}") for i in range(6)]
        path = _xmltv({"BandCampinas.br": "BAND CAMPINAS", "BandSP.br": "BAND SP"},
                      {"BandCampinas.br": rows, "BandSP.br": real})
        src = m.read_source_from_file(_spec(), path)
        self.assertNotIn("BandCampinas.br", src.rows)
        self.assertEqual(len(src.rows["BandSP.br"]), 6)

    def test_canal_preenchido_com_o_proprio_nome_fica_sem_grade(self):
        rows = [(T0 + timedelta(hours=i), T0 + timedelta(hours=i + 1), "BAND CAMPINAS") for i in range(8)]
        path = _xmltv({"BandCampinas.br": "BAND CAMPINAS"}, {"BandCampinas.br": rows})
        self.assertNotIn("BandCampinas.br", m.read_source_from_file(_spec(), path).rows)

    def _ev(self, key, rows, comparisons=()):
        els = []
        for start, stop, title in rows:
            el = ET.Element("programme", start=start.strftime("%Y%m%d%H%M%S +0000"),
                            stop=stop.strftime("%Y%m%d%H%M%S +0000"), channel="x")
            ET.SubElement(el, "title").text = title
            els.append(m.ProgrammeRow(start=start, stop=stop, title=title,
                                      normalized_title=m.normalize_programme_title(title), element=el))
        src = SimpleNamespace(spec=SimpleNamespace(key=key), rows={"c": els})
        return SimpleNamespace(match=SimpleNamespace(source=src, channel_id="c"), comparisons=list(comparisons))

    def _stitch(self, evs):
        return m.stitch_multi_source_schedule(evs, "BAND CAMPINAS", "BAND CAMPINAS",
                                              T0 - timedelta(hours=1), T0 + timedelta(hours=12), {})

    def test_candidata_nao_verificada_nao_preenche_lacuna(self):
        lider = self._ev("lider", [(T0, T0 + timedelta(hours=1), "Jornal"),
                                   (T0 + timedelta(hours=2), T0 + timedelta(hours=3), "Novela")],
                         comparisons=[{"other_source_key": "intrusa", "agreement": 0.05, "comparable": 20, "detected_shift": 0}])
        intrusa = self._ev("intrusa", [(T0 + timedelta(hours=1), T0 + timedelta(hours=2), "Resenha ESPN")])
        titulos = [e.findtext("title") for e in self._stitch([lider, intrusa])]
        self.assertEqual(titulos, ["Jornal", "Novela"])

    def test_candidata_sem_dados_comparaveis_tambem_fica_de_fora(self):
        lider = self._ev("lider", [(T0, T0 + timedelta(hours=1), "Jornal")], comparisons=[])
        outra = self._ev("outra", [(T0 + timedelta(hours=1), T0 + timedelta(hours=2), "Qualquer coisa")])
        self.assertEqual([e.findtext("title") for e in self._stitch([lider, outra])], ["Jornal"])

    def test_candidata_verificada_preenche_lacuna(self):
        lider = self._ev("lider", [(T0, T0 + timedelta(hours=1), "Jornal"),
                                   (T0 + timedelta(hours=2), T0 + timedelta(hours=3), "Novela")],
                         comparisons=[{"other_source_key": "amiga", "agreement": 0.9, "comparable": 20, "detected_shift": 0}])
        amiga = self._ev("amiga", [(T0 + timedelta(hours=1), T0 + timedelta(hours=2), "Boletim")])
        self.assertEqual([e.findtext("title") for e in self._stitch([lider, amiga])], ["Jornal", "Boletim", "Novela"])


if __name__ == "__main__":
    unittest.main()
