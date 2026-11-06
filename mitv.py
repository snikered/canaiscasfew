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

TRADUCOES = {
    'Action': 'Ação', 'Adventure': 'Aventura', 'Biography': 'Biografia', 'Bus./Financial': 'Negócios/Finanças',
    'Children': 'Infantil', 'Comedy': 'Comédia', 'Comedy Drama': 'Comédia Dramática', 'Community': 'Comunidade',
    'Consumer': 'Consumo', 'Dark Comedy': 'Comédia Negra', 'Documentary': 'Documentário', 'Drama': 'Drama',
    'Educational': 'Educativo', 'Entertainment': 'Entretenimento', 'Health': 'Saúde', 'History': 'História',
    'Horror': 'Terror', 'Interview': 'Entrevista', 'Music': 'Música', 'Nature': 'Natureza', 'News': 'Notícias',
    'Newsmagazine': 'Revista de Notícias', 'Politics': 'Política', 'Public Affairs': 'Assuntos Públicos',
    'Reality': 'Reality Show', 'Religious': 'Religioso', 'Romance': 'Romance', 'Science Fiction': 'Ficção Científica',
    'Shopping': 'Compras', 'Soap': 'Novela', 'Special': 'Especial', 'Sports Talk': 'Debate Esportivo',
    'Talk': 'Talk Show', 'Thriller': 'Suspense', 'Variety': 'Variedades',
}

def descobrir_canais():
    try:
        r = session.get('https://mi.tv/br/sitemap', timeout=15)
        soup = BeautifulSoup(r.content, 'html.parser')
        canais = {}
        for a in soup.find_all('a', href=True):
            if '/br/canais/' in a['href'] and a.get_text().strip() and a.get_text().strip() != '$nameFromProvider':
                canais[a.get_text().strip()] = a['href'].split('/')[-1]
        return canais
    except: return {}

def extrair_programacao(codigo, data_limite, hoje):
    eventos = []
    for dias in range(DIAS_PARA_FRENTE + 1):
        data_alvo = hoje + timedelta(days=dias)
        data_str = data_alvo.strftime('%Y-%m-%d')
        url = f'https://mi.tv/br/async/channel/{codigo}/-180' if dias == 0 else f'https://mi.tv/br/async/channel/{codigo}/{data_str}/-180'
        
        try:
            r = session.get(url, timeout=15)
            soup = BeautifulSoup(r.content, 'html.parser')
            for prog in soup.find_all('a', class_='program-link'):
                time_elem = prog.find('span', class_='time')
                hora = time_elem.get_text().strip() if time_elem else ''
                h2 = prog.find('h2')
                titulo_bruto = h2.get_text().strip() if h2 else ''
                tem_ao_vivo = h2.find('img', class_='vivo') is not None if h2 else False
                
                sub = prog.find('span', class_='sub-title')
                sub_texto = sub.get_text().strip() if sub else ''
                sinopse = prog.find('p', class_='synopsis')
                sinopse_texto = sinopse.get_text().strip() if sinopse else ''
                
                nota = ''
                if sub and (rating_elem := sub.find('span', class_='rating')):
                    nota = rating_elem.get_text().strip()
                    sub_texto = sub_texto.replace(nota, '').rstrip('/').strip()
                
                if not hora: continue
                try:
                    h, m = map(int, hora.split(':'))
                    inicio = data_alvo.replace(hour=h, minute=m, second=0, microsecond=0)
                    if inicio > data_limite: continue
                except: continue
                
                categorias, sub_title, ano = [], '', ''
                if 'Temporada' in sub_texto or 'Season' in sub_texto:
                    sub_title, categorias = sub_texto, ['Séries']
                elif '/' in sub_texto:
                    partes = sub_texto.split('/')
                    if len(partes) >= 1:
                        categorias.extend(['Filme', TRADUCOES.get(partes[0].strip(), partes[0].strip())])
                    if len(partes) >= 2 and (ano_match := re.search(r'\d{4}', partes[1])):
                        ano = ano_match.group(0)
                elif sub_texto:
                    categorias.append(TRADUCOES.get(sub_texto, sub_texto))
                
                eventos.append({
                    'inicio': inicio, 'titulo': f'Ao vivo - {titulo_bruto}' if tem_ao_vivo else titulo_bruto,
                    'categorias': categorias, 'sub_title': sub_title, 'ano': ano, 'nota': nota, 'sinopse': sinopse_texto
                })
        except: pass
    return eventos

def gerar_xml(caminho_saida):
    hoje = datetime.now()
    data_limite = hoje + timedelta(days=DIAS_PARA_FRENTE)
    canais = descobrir_canais()
    
    root = ET.Element('tv', {'generator-info-name': 'Scraper MiTV'})
    total_eventos, processados = 0, 0
    
    for nome, codigo in sorted(canais.items()):
        try:
            eventos = extrair_programacao(codigo, data_limite, hoje)
            if not eventos: continue
            
            eventos.sort(key=lambda x: x['inicio'])
            for i in range(len(eventos) - 1): eventos[i]['fim'] = eventos[i + 1]['inicio']
            eventos[-1]['fim'] = eventos[-1]['inicio'] + timedelta(hours=1)
            
            channel = ET.SubElement(root, 'channel', id=nome)
            ET.SubElement(channel, 'display-name').text = nome
            
            for ev in eventos:
                prog = ET.SubElement(root, 'programme', start=ev['inicio'].strftime('%Y%m%d%H%M%S -0300'), stop=ev['fim'].strftime('%Y%m%d%H%M%S -0300'), channel=nome)
                ET.SubElement(prog, 'title').text = ev['titulo']
                if ev['sub_title']: ET.SubElement(prog, 'sub-title').text = ev['sub_title']
                for cat in ev['categorias']: ET.SubElement(prog, 'category').text = cat
                if ev['ano']: ET.SubElement(prog, 'date').text = ev['ano']
                if ev['nota']:
                    rating = ET.SubElement(prog, 'rating', system='IMDB')
                    ET.SubElement(rating, 'value').text = ev['nota']
                if ev['sinopse']: ET.SubElement(prog, 'desc').text = ev['sinopse']
            
            total_eventos += len(eventos)
            processados += 1
            time.sleep(0.01)
        except: pass
    
    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding='utf-8', xml_declaration=True)
    print(f'MiTV -> Canais: {processados}/{len(canais)} | Eventos: {total_eventos}')

if __name__ == '__main__':
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'epg_mitv.xml')
    gerar_xml(caminho)