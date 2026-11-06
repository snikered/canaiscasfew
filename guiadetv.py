#!/usr/bin/env python3
import requests
import re
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import os
import time

DIAS_PARA_FRENTE = 4
BASE = 'https://www.guiadetv.com'
session = requests.Session()

# 1. Cabeçalhos modernos para contornar bloqueios a servidores do GitHub
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
    'Referer': 'https://www.google.com/'
})

CATEGORIAS = [
    'https://www.guiadetv.com/categorias/variedades.html', 'https://www.guiadetv.com/categorias/tv-aberta.html',
    'https://www.guiadetv.com/categorias/noticias.html', 'https://www.guiadetv.com/categorias/infantil.html',
    'https://www.guiadetv.com/categorias/filmes-e-series.html', 'https://www.guiadetv.com/categorias/esportes.html',
    'https://www.guiadetv.com/categorias/documentarios.html',
]

def descobrir_canais():
    canais = {}
    for url_cat in CATEGORIAS:
        try:
            r = session.get(url_cat, timeout=15)
            if r.status_code != 200:
                print(f"Aviso: O site retornou erro {r.status_code} na categoria {url_cat}")
                continue
                
            soup = BeautifulSoup(r.content, 'html.parser')
            for a in soup.find_all('a', href=re.compile(r'/canal/')):
                href = a['href']
                if href.startswith('//'): href = 'https:' + href
                elif href.startswith('/'): href = BASE + href
                
                nome = ' '.join(a.get_text().split())
                if nome and nome not in canais: 
                    canais[nome] = href
        except Exception as e:
            print(f"Erro ao acessar {url_cat}: {e}")
    return canais

def extrair_programacao(url_canal, data_limite):
    try:
        r = session.get(url_canal, timeout=15)
        if r.status_code != 200:
            return []
    except:
        return []
        
    soup = BeautifulSoup(r.content, 'html.parser')
    eventos = []
    
    # 2. Nova abordagem resistente a mudanças de HTML no site:
    # Procura diretamente os itens de lista que contêm um horário (time datetime=...)
    for li in soup.find_all('li'):
        time_tag = li.find('time', attrs={'datetime': True})
        if not time_tag: continue
        
        try: 
            inicio = datetime.strptime(time_tag['datetime'][:19], '%Y-%m-%dT%H:%M:%S')
        except: 
            continue
            
        if inicio > data_limite: 
            continue
            
        link_prog = li.find('a')
        if not link_prog: 
            continue
            
        titulo = ' '.join(link_prog.get_text().split())
        if not titulo: continue
        
        sub_titulo = ''
        if ' : ' in titulo: 
            titulo, sub_titulo = titulo.split(' : ', 1)
            
        duracao_min = 60
        if time_tag.parent and (span_dur := time_tag.parent.find('span')):
            txt_dur = ' '.join(span_dur.get_text().split())
            if match_dur := re.match(r'(\d+)\s*h\s*(\d+)?', txt_dur, re.IGNORECASE):
                duracao_min = int(match_dur.group(1)) * 60 + int(match_dur.group(2) or 0)
            elif match_dur := re.match(r'(\d+)\s*min', txt_dur, re.IGNORECASE):
                duracao_min = int(match_dur.group(1))
                
        badges = [' '.join(span.get_text().split()) for span in li.find_all('span') if not span.find('span') and len(' '.join(span.get_text().split())) < 30 and not re.match(r'^\d', ' '.join(span.get_text().split()))]
        tem_ao_vivo = any(b.lower() == 'ao vivo' for b in badges)
        
        sinopse = ''
        if p_sinopse := li.find('p'):
            sinopse = ' '.join(p_sinopse.get_text().split())
            if 'sinopse n' in sinopse.lower()[:12]: sinopse = ''
            
        evento = {'inicio': inicio, 'fim': inicio + timedelta(minutes=duracao_min), 'titulo': 'Ao vivo - ' + titulo if tem_ao_vivo else titulo}
        if sub_titulo: evento['sub_titulo'] = sub_titulo
        if sinopse: evento['desc'] = sinopse
        eventos.append(evento)
            
    eventos.sort(key=lambda x: x['inicio'])
    return eventos

def gerar_xml(caminho_saida):
    hoje = datetime.now()
    data_limite = hoje + timedelta(days=DIAS_PARA_FRENTE)
    
    print("Iniciando varredura do GuiaDeTV...")
    canais = descobrir_canais()
    print(f"Encontrados {len(canais)} canais para processar.")
    
    root = ET.Element('tv', {'generator-info-name': 'Scraper GuiaDeTV'})
    total_eventos, processados = 0, 0

    for nome, url in sorted(canais.items()):
        eventos = extrair_programacao(url, data_limite)
        if not eventos: 
            continue
            
        eventos.sort(key=lambda x: x['inicio'])
        for i in range(len(eventos) - 1):
            if eventos[i]['fim'] > eventos[i + 1]['inicio']: 
                eventos[i]['fim'] = eventos[i + 1]['inicio']

        channel = ET.SubElement(root, 'channel', id=nome)
        ET.SubElement(channel, 'display-name').text = nome

        for ev in eventos:
            prog = ET.SubElement(root, 'programme', start=ev['inicio'].strftime('%Y%m%d%H%M%S -0300'), stop=ev['fim'].strftime('%Y%m%d%H%M%S -0300'), channel=nome)
            ET.SubElement(prog, 'title').text = ev['titulo']
            if ev.get('sub_titulo'): ET.SubElement(prog, 'sub-title').text = ev['sub_titulo']
            if 'desc' in ev: ET.SubElement(prog, 'desc').text = ev['desc']
            
        total_eventos += len(eventos)
        processados += 1
        time.sleep(0.1)

    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding='utf-8', xml_declaration=True)
    print(f'GuiaDeTV -> Canais processados com sucesso: {processados}/{len(canais)} | Total de Eventos: {total_eventos}')

if __name__ == '__main__':
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'epg_guiadetv.xml')
    gerar_xml(caminho)
