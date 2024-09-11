from elasticsearch import Elasticsearch
from flask import Flask, render_template, request, Response, abort, jsonify
import requests
from bs4 import BeautifulSoup
import pymongo
import os
from bson.objectid import ObjectId
import re

app = Flask(__name__)

# Elasticsearch bağlantısı
es = Elasticsearch([{'host': 'localhost', 'port': 9200, 'scheme': 'http'}])

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/tara', methods=['POST'])
def tara():
    if request.method == 'POST':
        sorgu = request.form['sorgu']
        sayfa_sayisi = int(request.form['sayfa_sayisi'])

        veri_listesi = makaleleri_tara(sorgu, sayfa_sayisi)

        # MongoDB'ye veri kaydı
        client = pymongo.MongoClient("mongodb://localhost:27017/")
        db = client["PythonDatabase"]
        collection = db["scholar_veriler"]

        for veri in veri_listesi:
            collection.insert_one(veri)

            # "_id" alanını hariç tutarak Elasticsearch'e veri kaydı
            veri_kopya = veri.copy()  # Orijinal veri değişmeden kopya oluşturuyoruz
            if '_id' in veri_kopya:
                del veri_kopya['_id']  # "_id" alanını sil
            
            es.index(index='scholar_veriler', body=veri_kopya)

        return render_template('sonuc.html', veri_listesi=veri_listesi)
    
@app.route('/search', methods=['POST'])
def search():
    if request.method == 'POST':
        anahtar_kelime = request.form['anahtar_kelime']
        filtre = request.form.get('filtre')  # Kullanıcının seçtiği filtre (tarih, atıf)

        # Elasticsearch sorgusu
        sorgu = {
            "query": {
                "multi_match": {
                    "query": anahtar_kelime,
                    "fields": ["Yayın Adı", "Yazar Adı", "Anahtar Kelimeler"],
                    "fuzziness": "AUTO"  # Yazım yanlışlarını düzeltme
                }
            },
            "sort": [],
            "suggest": {
                "text": anahtar_kelime,
                "simple_phrase": {
                    "phrase": {
                        "field": "Yayın Adı.suggest",
                        "size": 1,
                        "gram_size": 3,
                        "direct_generator": [{
                            "field": "Yayın Adı.suggest",
                            "suggest_mode": "always"
                        }]
                    }
                }
            }
        }

        # Sıralama ekleme
        if filtre == "tarih":
            sorgu["sort"].append({"Yayımlanma Tarihi": {"order": "desc"}})
        elif filtre == "atıf":
            sorgu["sort"].append({"Atıf Sayısı": {"order": "desc"}})

        # Elasticsearch'te arama yap
        response = es.search(index="scholar_veriler", body=sorgu)
        suggestions = response.get('suggest', {}).get('simple_phrase', [])[0]['options']
        corrected = suggestions[0]['text'] if suggestions else anahtar_kelime

        # Sonuçları ve önerileri bir HTML sayfasında göstermek için hazırla
        return render_template('sonuc.html', original=anahtar_kelime, corrected=corrected, results=response['hits']['hits'])

def makaleleri_tara(sorgu, sayfa_sayisi):
    veri_listesi = []
    for sayfa in range(sayfa_sayisi):
        url = f"https://scholar.google.com/scholar?start={sayfa*10}&q={sorgu}&hl=tr&as_sdt=0,5"
        response = requests.get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        results = soup.find_all("div", class_="gs_r gs_or gs_scl")
        for result in results:
            title = result.find("h3", class_="gs_rt").text.strip()
            link = result.find("a")["href"]
            publication_info = result.find("div", class_="gs_a").text.strip()
            publish_year = yayin_yilini_bul(publication_info)  # Yayın yılını çıkar
            publish_date = f"{publish_year}-01-01" if publish_year else None  # Yayımlanma tarihi ISO8601 formatına dönüştürülüyor
            alinti_element = result.find("div", class_="gs_fl gs_flb")
            links = alinti_element.find_all("a")
            alinti_sayisi = [link.text for link in links if "Alıntılanma sayısı" in link.text][0]  # Direkt metni al
 
            pdf_link = None
            pdf_element = result.find("div", class_="gs_or_ggsm")
            if pdf_element and pdf_element.find("a"):
                potential_pdf_link = pdf_element.find("a")["href"]
                if potential_pdf_link.endswith(".pdf"):
                    pdf_link = potential_pdf_link  # Doğrudan PDF linkini kullan

            veri_listesi.append({
                "Yayın Adı": title,
                "Yazar Adı ve Yayın Yılı": publication_info,
                "Yayımlanma Tarihi": publish_date,  
                "Alıntılanma Sayısı": alinti_sayisi,
                "URL": link,
                "PDF Linki": pdf_link
            })
    return veri_listesi

@app.route('/indir_pdf')
def indir_pdf():
    url = request.args.get('url')
    if not url:
        return Response("PDF linki sağlanmadı.", status=400)

    try:
        response = requests.get(url)
        if response.status_code == 200 and 'application/pdf' in response.headers['Content-Type']:
            return Response(
                response.content,
                headers={
                    'Content-Disposition': 'attachment; filename="downloaded.pdf"',
                    'Content-Type': 'application/pdf'
                }
            )
        else:
            return Response("İndirilen içerik bir PDF değil veya erişilemez.", status=415)
    except requests.exceptions.RequestException as e:
        return Response(f"Bir hata oluştu: {e}", status=500)
    
@app.route('/article_detail/<article_id>')
def article_detail(article_id):
    try:
        client = pymongo.MongoClient("mongodb://localhost:27017/")
        db = client["PythonDatabase"]
        collection = db["scholar_veriler"]

        # MongoDB'den '_id' ile veri çekme, ObjectId kullanarak
        article_data = collection.find_one({'_id': ObjectId(article_id)})

        if article_data:
            # MongoDB'den gelen veriyi JSON'a çevirme
            article_data['_id'] = str(article_data['_id'])  # ObjectId'yi string'e çevir
            return jsonify(article_data)
        else:
            return jsonify({"error": "Makale bulunamadı"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
def yayin_yilini_bul(yayin_bilgisi):
    # Regex ile metin içerisinden dört basamaklı sayıyı bul (genellikle yıl bu şekildedir)
    yil_bul = re.search(r'\b(19|20)\d{2}\b', yayin_bilgisi)
    if yil_bul:
        return yil_bul.group(0)  # Yılı döndür
    return None  # Eğer yıl bulunamazsa None döndür
    
# Dosya adını temizleme fonksiyonu
def dosya_adi_temizle(baslik):
    return re.sub(r'[<>:"/\\|?*]', '', baslik)

if __name__ == '__main__':
    app.run(debug=True)
