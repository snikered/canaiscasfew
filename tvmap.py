#!/usr/bin/env python3
import requests
import json
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import os

DIAS_PARA_FRENTE = 4
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})

def pegar_grade(url_grid=None):
    url = f'https://tvmap.com.br/api/Programacao?availWidth=2560&grid={url_grid}' if url_grid else 'https://tvmap.com.br/api/Programacao?availWidth=2560'
    r = session.get(url, timeout=15)
    return r.json()

def extrair_todos():
    todos_canais = {}
    url_grid = None
    paginas = 0
    while paginas < 50:
        try: data = pegar_grade(url_grid)
        except: break
        for ch in data.get('channels', []):
            nome = ch.get('displayName', '')
            if nome not in todos_canais: todos_canais[nome] = []
            todos_canais[nome].extend(ch.get('exhibitions', []))
        next_grid = data.get('nextGridUrl', '')
        if not next_grid: break
        url_grid = next_grid
        paginas += 1
    return todos_canais

def gerar_xml(caminho_saida):
    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    data_limite = hoje + timedelta(days=DIAS_PARA_FRENTE)
    todos_canais = extrair_todos()
    
    root = ET.Element('tv', {'generator-info-name': 'Scraper TVMap'})
    total_eventos = 0
    
    for nome, exhibitions in sorted(todos_canais.items()):
        channel = ET.SubElement(root, 'channel', id=nome)
        ET.SubElement(channel, 'display-name').text = nome
        for ex in exhibitions:
            start_str, end_str = ex.get('exhibitionStartDate', ''), ex.get('exhibitionCalculatedEndDate', '')
            if not start_str or not end_str: continue
            try:
                inicio = datetime.fromisoformat(start_str).replace(tzinfo=None)
                fim = datetime.fromisoformat(end_str).replace(tzinfo=None)
            except: continue
            
            if inicio < hoje or inicio > data_limite: continue
            
            p = ET.SubElement(root, 'programme', start=inicio.strftime('%Y%m%d%H%M%S') + ' -0300', stop=fim.strftime('%Y%m%d%H%M%S') + ' -0300', channel=nome)
            ET.SubElement(p, 'title').text = ex.get('title', '')
            desc_texto = ex.get('description', '')
            if desc_texto: ET.SubElement(p, 'desc').text = desc_texto
            total_eventos += 1
            
    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding='utf-8', xml_declaration=True)
    print(f'TVMap -> Canais: {len(todos_canais)} | Eventos: {total_eventos}')

if __name__ == '__main__':
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'epg_tvmap.xml')
    gerar_xml(caminho)