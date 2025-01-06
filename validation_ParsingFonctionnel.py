#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Created By  : Etienne COTTRELLE
# Date & version : v1.0 19/06/2024
# ---------------------------------------------------------------------------
""" Script de parsing du Monitoring Fonctionnel
Objectif : Parser les données élément par élément pour transmission vers Ovale (ElasticSearch) en respectant un modèle de donnée établi
Features : 
    - lis les formats CSV et XML et detecte l'encodage du fichier
    - a partir d'un fichier de conf : émet dynamiquement des champs keyword, number et date 
      ainsi que l'id transactionnel necessaire au monitoring fonctionnel
    - émet une requete PUT via l'API mise à disposition par OVale
    - convertis tout format de date en UTC standard d'Ovale
    - archive les fichiers après traitement
    - trace la donnée telle qu'elle, n'a pas vocation à réparer ou alerter
    - seul controle sur la donnée : si l'id transactionnel est incomplet il affichera erreur
Paramètres :
    env = environnement d'origine du fichier
    process_name = nom du process observé
    step = étape du process
    file_name = optionnel, nom du fichier à traiter. En l'absence de ce paramètre tout les fichiers du dossier seront traité
"""
import os
import sys
import shutil
from datetime import datetime
from dateutil import parser
import pytz
import requests
import chardet
import csv
from lxml import etree 
import json
import logging
# Configure logging
log_dir = os.path.join(os.environ['DATA_MONITORING'], 'log')
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

class Entry:
    def __init__(self, env, process_name, step):
        """
        Initialise les éléments de l'entrée déstiné à Ovale en lisant le fichier de configuration
        """
        try:
            with open (config_path) as config_file:
                config = json.load(config_file)

            self.champs = config[env]["etapes"][step]["champs"]
            self.fileType = config[env]["etapes"][step]["type"]
            self.processName = config[env]["etapes"][step]["processus"]["code"]
            self.environnement = config[env]["etapes"][step]["environnement"]
            self.appliCassiniCode = config[env]["etapes"][step]["application"]["cassini"]
            self.appliCassiniName = config[env]["etapes"][step]["application"]["nom"]
            self.emetteurCassiniCode = config[env]["etapes"][step]["emetteur"]["cassini"]
            self.emetteurCassiniName = config[env]["etapes"][step]["emetteur"]["nom"]
            self.stepId = config[env]["etapes"][step]["processus"]["etape"]["id"]
            self.stepName = config[env]["etapes"][step]["processus"]["etape"]["nom"]
            self.stepStatutConfig = config[env]["etapes"][step]["processus"]["etape"]["statut"]
            self.transactionId_fields = config[env]["etapes"][step]["champs"]["trace_id"]
            self.transactionId = ""
            self.statut = self.stepStatutConfig["defaut"]
            self.xmlNamespace = None
            self.data = {}
        except Exception as e:
            logging.error(f"Erreur le paramétrage désigné n'existe pas pour environnement : {env} , process : {process_name} , étape : {step} .")
            raise

    def extract_from_csv(self, row):
        """
        Dans le cas d'un fichier CSV, extraits les champs comme indiqué dans le fichier de configuration
        complète l'attribut self.data
        """
        try:
            for key, col_index in self.champs.get("keywords", {}).items():
                self.data[key] = row[col_index]

            for key, col_index in self.champs.get("nombres", {}).items():
                value = str(row[col_index]).replace(',', '.')
                self.data[key] = float(value)

            for key, col_index in self.champs.get("dates", {}).items():
                self.data[key] = convert_date_to_UTC(row[col_index])
            
            logging.debug(f"data extraite : {self.data}")
        except Exception as e:
            logging.error(f"Erreur d'extraction : {row} - {str(e)}")


    def extract_from_xml(self, element):
        """
        Dans le cas d'un fichier XML, extraits les champs comme indiqué dans le fichier de configuration
        complète l'attribut self.data
        """
        try:
            for key, value_pathXml in self.champs.get("keywords", {}).items():
                self.data[key] = element.xpath(f'{value_pathXml}/text()', namespaces=self.xmlNamespace)[0]

            for key, value_pathXml in self.champs.get("nombres", {}).items():
                value = str(element.xpath(f'{value_pathXml}/text()', namespaces=self.xmlNamespace)[0])
                self.data[key] = float(value)

            for key, value_pathXml in self.champs.get("dates", {}).items():
                self.data[key] = convert_date_to_UTC(element.xpath(f'{value_pathXml}/text()', namespaces=self.xmlNamespace)[0])
            # print(f"data extraite : {self.data}")
            logging.debug(f"data extraite : {self.data}")
        except Exception as e:
            logging.error(f"Erreur d'extraction : {element} - {str(e)}")

    def create_transactionId(self, element, is_csv):
        """
        A partir des champs désigné dans self.transactionId_fields, construit l'id transactionnel de la data
        """
        id_parts = []

        if is_csv:
            for field_type, keys in self.transactionId_fields.items():
                for key in keys:
                    col_index = self.champs[field_type][key]
                    if len(str(element[col_index])) > 0:
                        id_parts.append(str(element[col_index]))
                    else:
                        id_parts.append("ERREUR")
        else:
            for field_type, keys in self.transactionId_fields.items():
                for key in keys:
                    value_pathXml = self.champs[field_type][key]
                    value = element.xpath(f'{value_pathXml}/text()', namespaces=self.xmlNamespace)[0]
                    id_parts.append(value if value else "ERREUR")

        self.transactionId = "_".join(id_parts)
        logging.debug(f"id transactionnel : {self.transactionId}")

    def setStatut(self, element, is_csv):
        """
        A partir des champs désigné dans self.stepStatutConfig, determine le statut de la transaction
        """
        if self.stepStatutConfig["indicateur"]:
            if is_csv:
                col_index = self.stepStatutConfig["indicateur"]
                if element[col_index] is not self.stepStatutConfig["defaut"]:
                    self.statut = "KO" 
            else:
                value_pathXml = self.stepStatutConfig["indicateur"]
                value = element.xpath(f'{value_pathXml}/text()', namespaces=self.xmlNamespace)[0]
                if value is not self.stepStatutConfig["defaut"]:
                    self.statut = "KO" 

        logging.debug(f"id transactionnel : {self.statut}")
    
    def create_message(self):
        """
        Crée le message tel qu'attendu par le modèle de donnée d'Ovale
        """
        # décommenter le fuseau horaire adapté
        # fuseau horaire de Paris
        currentTimezone = pytz.timezone('Europe/Paris')
        # fuseau horaire UTC +00 dans tout les cas
        # currentTimezone = pytz.utc
        now_tz=datetime.now(tz=currentTimezone)

        message = {
            "@timestamp": now_tz.strftime('%Y-%m-%dT%H:%M:%S'),
            "ag2rlm": {
                "application":{
                    "cassini": self.appliCassiniCode,
                    "nom": self.appliCassiniName
                },
                "emetteur": { 
                    "cassini": self.emetteurCassiniCode,
                    "nom": self.emetteurCassiniName
                    },
                "environnement":{
                    "nom": self.environnement,
                },
                "informations_metier": self.data,
                "processus": {
                    "code": self.processName,
                    "etape": {
                        "id": self.stepId,
                        "nom": self.stepName,
                        "statut": self.statut
                    }
                }
            },
            "trace": { 
                "id": self.transactionId
            } 
        }
        return message

def put_api(url, message, headers=None):
    """
    requête PUT sur l'URL spécifié avec le message fourni

    url = url de l'API
    message = dictionnaire de données à envoyer
    header = par défaut JSON 
    return = la réponse de l'API consultable avec response.status_code et response.text
    """
    try:
        if headers is None:
            headers = {"Content-Type": "application/json"}
        else:
            headers["Content-Type"] = "application/json"
        
        response = requests.put(url, headers=headers, data=json.dumps(message))
        logging.debug(f"Envoi du message : {message} vers Ovale")
        return response
    except Exception as e:
        print(f"Erreur d'envoi du message : {message} vers Ovale")
        logging.error(f"Erreur d'envoi du message : {message} vers Ovale")
        raise

def process_csv(file_path, env, process_name, step):
    """
    Si le fichier est un CSV, envoi un message vers Ovale par ligne
    """
    encoding = detect_encoding(file_path)
    try:
        with open(file_path, encoding=encoding, newline='') as csvfile:
            reader = csv.reader(csvfile, delimiter=';')
            header = next(reader, None) # passe la ligne header si elle existe
            if header is None:
                print(f"Fichier vide: {file_path}")
                logging.error(f"Fichier vide: {file_path}")
                return
            for row in reader:
                entry = Entry(env, process_name, step)
                entry.create_transactionId(row, is_csv=True)
                entry.setStatut(row, is_csv=True)
                entry.extract_from_csv(row)
                message = entry.create_message()
                # print(message)
                put_api(url_api, message)
    except UnicodeDecodeError:
        logging.error(f"Impossible de décoder {file_path} avec l'encodage {encoding}")
        raise ValueError(f"Impossible de décoder {file_path} avec l'encodage {encoding}")
    except Exception as e:
        logging.error(f"Erreur dans le traitement du fichier CSV : {file_path}")
        print((f"Erreur dans le traitement du fichier CSV : {file_path}"))
        raise

def process_xml(file_path, env, process_name, step, xmlPath):
    """
    Si le fichier est un XML, envoi un message vers Ovale par element
    """
    start_time = datetime.now()
    try:
        tree = etree.parse(file_path)
        root = tree.getroot()
        namespace = get_namespace(root.tag)
        elements = root.findall(f'{xmlPath}', namespace)
        if not elements:
            print(f"Fichier vide: {file_path}")
            logging.error(f"Fichier vide: {file_path}")
            return
        for element in elements:
            entry = Entry(env, process_name, step)
            entry.xmlNamespace = namespace
            entry.create_transactionId(element, is_csv=False)
            entry.setStatut(element, is_csv=False)
            entry.extract_from_xml(element)
            message = entry.create_message()
            # print(message)
            put_api(url_api, message)
    except Exception as e:
        logging.error(f"Erreur dans le traitement du fichier XML : {file_path}")
        print(f"Erreur dans le traitement du fichier XML : {file_path}")
        raise
    finally:
        end_time = datetime.now()
        logging.info(f"Fichié traité en {end_time - start_time}")

def process_files(env, process_name, step, file_name=None):
    """
    Process les fichiers dans le repertoire assigné au process_name ou le fichier désigné par file_name.
    Determine par la config du process le type de fichier et appelle la fonction de process adapté
    """
    start_time = datetime.now()
    print(f"Traitement du process {process_name}, environnement {env} étape {step}")
    logging.info(f"Traitement du process {process_name}, environnement {env} étape {step}")
    logging.info(f"Debut de traitement du process {process_name}, environnement {env} étape {step} à {start_time.strftime('%Y-%m-%dT%H:%M:%S')}")
    entry = Entry(env, process_name, step)
    try:
        if file_name:
            files_to_process = [os.path.join(data_path, file_name)]
        else:
            files_to_process = [os.path.join(data_path, f) for f in os.listdir(data_path) if os.path.isfile(os.path.join(data_path, f))]

        for file_path in files_to_process:
            print(f"Parsing du fichier {file_path}")
            logging.info(f"Parsing du fichier {file_path}")
            if entry.fileType == "csv":
                process_csv(file_path, env, process_name, step)
            elif entry.fileType == "xml":
                process_xml(file_path, env, process_name, step, entry.champs["pathXML"])
            move_file(file_path, os.path.join(os.path.dirname(file_path), 'archive'))
        logging.info(f"Parsing effectué avec succès")
    except Exception as e:
        print(f"Erreur dans le parsing des fichiers: {str(e)}")
        logging.error(f"Erreur dans le parsing des fichiers: {str(e)}")
        raise
    finally:
        end_time= datetime.now()
        logging.info(f"Fin traitement du process {process_name}, environnement {env} étape {step}")
        print(f"Traitement terminé à {end_time.strftime('%Y-%m-%dT%H:%M:%S')}")
        logging.info(f"Traitement terminé à {end_time.strftime('%Y-%m-%dT%H:%M:%S')}")
        print(f"Temps de traitement total: {end_time - start_time} pour {len(files_to_process)} fichiers")
        logging.info(f"Temps de traitement total: {end_time - start_time} pour {len(files_to_process)} fichiers")

    
def move_file(file_path, target):
    """
    Déplace un fichier vers la target
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Le fichier '{file_path}' n'existe pas")
    if not os.path.isdir(target):
        try:
            os.makedirs(target)
            print(f"le repertoire de destination '{target}' n'existait pas, il a été créé")
        except:
            raise NotADirectoryError(f"le repertoire de destination '{target}' n'existe pas et impossible de le créer")
    
    shutil.move(file_path, target)
    print(f"Le fichier '{file_path}' a été déplacé vers '{target}'")

def detect_encoding(file_path):
    """
    Lis les X premiers bytes d'un fichier pour determiner l'encodage du fichier
    Limiter le nombre de bytes permet d'économiser du temps processeur
    Si le caractère mal encodé est après les X premiers bytes cette fonction ne rend pas service
    """
    try:
        with open(file_path, 'rb') as f:
            raw_data = f.read(10000)
            result = chardet.detect(raw_data)
            encoding = result['encoding']
        return encoding
    except FileNotFoundError as fnfe:
        print(f"Fichier non trouvé: {fnfe}")
        logging.error(f"Fichier non trouvé: {fnfe}")
        raise

def get_namespace(rootTag):
    """
    S'il existe, identifie l'espace de noms présent dans la racine d'un document XML.
    S'il n'existe pas, return None
    """
    namespace = rootTag[rootTag.find("{")+1 : rootTag.find("}")]
    if namespace:
        return {'ns': namespace}
    else:
        return None

def convert_date_to_UTC(date_str):
    """
    Convertie n'importe quelle date donnée en format texte vers UTC de Parsi
    et formatte en ISO 8601
    """
    try:
        # Analyse de la date donnée
        parsed_date = parser.parse(date_str)
        # Convertir la date en fuseau horaire Europe/Paris
        paris_tz = pytz.timezone('Europe/Paris')
        localized_date = paris_tz.localize(parsed_date, is_dst=None)
        # Convertir en UTC
        utc_date = localized_date.astimezone(pytz.utc)

        return utc_date.strftime('%Y-%m-%dT%H:%M:%S')

    except ValueError:
        # Si la date n'est pas valide, retourner None ou lever une exception
        return None

if __name__ == "__main__":

    logging.basicConfig(
    filename = os.path.join(log_dir, f"{sys.argv[1]}_{sys.argv[2]}_{sys.argv[3]}_{datetime.now().strftime('%Y%m%d%H%M%S')}.log"),
    level = logging.DEBUG,
    format = '%(asctime)s - %(levelname)s - %(message)s',
    datefmt = '%Y-%m-%d %H:%M:%S'
    )
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} environnement process_name etape [file_name]")
        logging.error(f"Usage: {sys.argv[0]} environnement process_name etape [file_name]")
        sys.exit(1)

    # Assignation des variables
    env = sys.argv[1]
    process_name = sys.argv[2]
    step = sys.argv[3]
    file_name = sys.argv[4] if len(sys.argv) > 4 else None
    config_path = os.path.join(os.environ['PATH_MONITORING'], f'configs/{process_name}.json')
    data_path = os.path.join(os.environ['DATA_MONITORING'], f'{env}/{process_name}/{step}')
    # variabilisation du endpoint de l'API Ovale en fonction du hostname de la machine
    if os.uname()[1] == "lpa2iab9.server.lan":
        # assigner le endpoint API Ovale de prod
        url_api = "http://lta1ia02:8002/toto_toto"
        print(f"endpoint api Ovale en prod : {url_api} .")
        logging.info(f"endpoint api Ovale en prod : {url_api} .")
    else:
        # assigner le endpoint API du lab Ovale
        url_api = "http://lta1ia02:8002/toto_toto"
        print(f"endpoint api Ovale en dev : {url_api} .")
        logging.info(f"endpoint api Ovale en dev : {url_api} .")

    process_files(env, process_name, step, file_name)
