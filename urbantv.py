#!/usr/bin/env python3
import requests
import time
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import os

DIAS_PARA_FRENTE = 4
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})

CANAIS_PTBR = [
    ('Otaku', 'otaku'), ('Urban Docs', 'docs'), ('Urban Drive', 'drive'),
    ('Urban Kids', 'kids'), ('Urban Movie', 'movie'), ('Urban Retro', 'retro'),
    ('Urban Series', 'series'), ('Urban Travel', 'travel'), ('ZooKids', 'zookids'),
    ('Moove Cine', 'moove-cine'), ('Moove Cult', 'moove-cult'),
    ('Moove Explore', 'moove-explore'), ('Villa Kids', 'villa-kids'),
]

def extrair_canal(nome, slug):
    timestamp = int(time.time() * 1000)
    url = f'https://urbantv.com.br/grade/data/{slug}.json?t={timestamp}'
    return session.get(url, timeout=15).json().get('programs', [])

def gerar_xml(caminho_saida):
    root = ET.Element('tv', {'generator-info-name': 'Scraper UrbanTV'})
    hoje_inicio = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    data_limite = hoje_inicio + timedelta(days=DIAS_PARA_FRENTE)
    total_eventos = 0
    
    for nome, slug in CANAIS_PTBR:
        try:
            programs = extrair_canal(nome, slug)
            channel = ET.SubElement(root, 'channel', id=nome)
            ET.SubElement(channel, 'display-name').text = nome
            
            for prog in programs:
                start_brasil = (datetime.fromisoformat(prog['start'].replace('Z', '+00:00')) - timedelta(hours=3)).replace(tzinfo=None)
                stop_brasil = (datetime.fromisoformat(prog['stop'].replace('Z', '+00:00')) - timedelta(hours=3)).replace(tzinfo=None)
                
                if start_brasil < hoje_inicio or start_brasil > data_limite: continue
                
                p = ET.SubElement(root, 'programme', start=start_brasil.strftime('%Y%m%d%H%M%S') + ' -0300', stop=stop_brasil.strftime('%Y%m%d%H%M%S') + ' -0300', channel=nome)
                ET.SubElement(p, 'title').text = prog['title']
                
                desc_texto = prog.get('description', '')
                if desc_texto: ET.SubElement(p, 'desc').text = desc_texto + ('...' if not desc_texto.endswith('.') else '')
                if prog.get('year'): ET.SubElement(p, 'date').text = prog['year']
                if prog.get('actors'):
                    credits = ET.SubElement(p, 'credits')
                    ET.SubElement(credits, 'actor').text = ', '.join(prog['actors'])
                total_eventos += 1
        except: pass
    
    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding='utf-8', xml_declaration=True)
    print(f'UrbanTV -> Canais processados | Eventos: {total_eventos}')

if __name__ == '__main__':
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'epg_urbantv.xml')
    gerar_xml(caminho)