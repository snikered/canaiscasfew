#!/usr/bin/env python3
import requests
import re
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import os
import time

DIAS_PARA_FRENTE = 4
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})

CATEGORIAS = [
    'https://meuguia.tv/programacao/categoria/Filmes', 'https://meuguia.tv/programacao/categoria/Series',
    'https://meuguia.tv/programacao/categoria/Esportes', 'https://meuguia.tv/programacao/categoria/Infantil',
    'https://meuguia.tv/programacao/categoria/Variedades', 'https://meuguia.tv/programacao/categoria/Documentarios',
    'https://meuguia.tv/programacao/categoria/Noticias', 'https://meuguia.tv/programacao/categoria/Aberta',
]

def descobrir_canais():
    canais = {}
    for url_cat in CATEGORIAS:
        try:
            r = session.get(url_cat, timeout=15)
            soup = BeautifulSoup(r.content, 'html.parser')
            for a in soup.find_all('a', href=True):
                if '/programacao/canal/' in a['href']:
                    nome = a.get_text().strip().split('\n')[0].strip()
                    if nome and nome not in canais: canais[nome] = a['href']
        except: pass
    return canais

def extrair_programacao(url_canal, data_limite, hoje):
    if url_canal.startswith('/'): url_canal = 'https://meuguia.tv' + url_canal
    r = session.get(url_canal, timeout=15)
    linhas = [l.strip() for l in BeautifulSoup(r.content, 'html.parser').get_text().split('\n') if l.strip()]
    eventos, data_atual = [], None

    for i, linha in enumerate(linhas):
        match_dia = re.match(r'^(\w+(?:-feira)?),\s*(\d+)/(\d+)', linha, re.IGNORECASE)
        if match_dia:
            dia_num, mes_num = int(match_dia.group(2)), int(match_dia.group(3))
            ano = hoje.year + 1 if mes_num < hoje.month else hoje.year
            data_atual = datetime(ano, mes_num, dia_num)
            continue
        match_hora = re.match(r'^(\d{2}:\d{2})$', linha)
        if match_hora and data_atual:
            h, m = map(int, match_hora.group(1).split(':'))
            inicio = data_atual.replace(hour=h, minute=m, second=0, microsecond=0)
            if inicio > data_limite: continue
            titulo = linhas[i + 1] if i + 1 < len(linhas) else ''
            genero = linhas[i + 2] if i + 2 < len(linhas) and '/' in linhas[i + 2] and not linhas[i + 2].startswith('Publicidade') else ''
            if titulo and not titulo.startswith('Publicidade'):
                eventos.append({'inicio': inicio, 'titulo': titulo, 'genero': genero})
    return eventos

def gerar_xml(caminho_saida):
    hoje = datetime.now()
    data_limite = hoje + timedelta(days=DIAS_PARA_FRENTE)
    canais = descobrir_canais()
    
    root = ET.Element('tv', {'generator-info-name': 'Scraper MeuGuia'})
    total_eventos, processados = 0, 0

    for nome, url in sorted(canais.items()):
        try:
            eventos = extrair_programacao(url, data_limite, hoje)
            if not eventos: continue
            
            eventos.sort(key=lambda x: x['inicio'])
            for i in range(len(eventos) - 1): eventos[i]['fim'] = eventos[i + 1]['inicio']
            eventos[-1]['fim'] = eventos[-1]['inicio'] + timedelta(hours=1)

            channel = ET.SubElement(root, 'channel', id=nome)
            ET.SubElement(channel, 'display-name').text = nome

            for ev in eventos:
                prog = ET.SubElement(root, 'programme', start=ev['inicio'].strftime('%Y%m%d%H%M%S -0300'), stop=ev['fim'].strftime('%Y%m%d%H%M%S -0300'), channel=nome)
                ET.SubElement(prog, 'title').text = ev['titulo']
                if ev['genero']:
                    for p in ev['genero'].split('/'):
                        if p.strip(): ET.SubElement(prog, 'category').text = p.strip()
            total_eventos += len(eventos)
            processados += 1
            time.sleep(0.02)
        except: pass

    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding='utf-8', xml_declaration=True)
    print(f'MeuGuia -> Canais: {processados}/{len(canais)} | Eventos: {total_eventos}')

if __name__ == '__main__':
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'epg_meuguia.xml')
    gerar_xml(caminho)