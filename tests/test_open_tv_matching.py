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


if __name__ == "__main__":
    unittest.main()
