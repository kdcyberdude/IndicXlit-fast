import os
import re
import tqdm
import ujson
from pydload import dload
import zipfile
from abc import ABC, abstractmethod, abstractproperty
from indicnlp.normalize.indic_normalize import IndicNormalizerFactory
from urduhack import normalize as shahmukhi_normalize
from ..utils import *
import json

LANG_WORD_REGEXES = {
    lang_name: re.compile(
        f"[{SCRIPT_CODE_TO_UNICODE_CHARS_RANGE_STR[script_name]}]+")
    for lang_name, script_name in LANG_CODE_TO_SCRIPT_CODE.items()
}

MODEL_FILE = 'transformer/indicxlit.pt'
DICTS_FOLDER = 'word_prob_dicts'
CHARS_FOLDER = 'corpus-bin'
DICT_FILE_FORMAT = '%s_word_prob_dict.json'
LANG_LIST_FILE = '../lang_list.txt'

normalizer_factory = IndicNormalizerFactory()

# Create a dictionary to store the transliterated words mapping
tranliterated_words_map = {}

# Specify the path to the cache file
cache_file_path = '/home/kd/Desktop/proj/dec/IndicXlit/tranliterated_words_map.json'

with open(cache_file_path, 'r') as f:
    tranliterated_words_map = json.load(f)


class BaseEngineTransformer(ABC):

    @abstractproperty
    def all_supported_langs(self):
        pass

    @abstractproperty
    def tgt_langs(self):
        pass

    def __init__(self, models_path, beam_width, rescore, batch_size):
        # added by yash

        print("Initializing Multilingual model for transliteration")
        if 'en' in self.tgt_langs:
            lang_pairs_csv = ','.join(
                [lang+"-en" for lang in self.all_supported_langs])
        else:
            lang_pairs_csv = ','.join(
                ["en-"+lang for lang in self.all_supported_langs])

        # initialize the model
        from .custom_interactive import Transliterator
        self.transliterator = Transliterator(
            os.path.join(models_path, CHARS_FOLDER),
            os.path.join(models_path, MODEL_FILE),
            lang_pairs_csv=lang_pairs_csv,
            lang_list_file=os.path.join(models_path, LANG_LIST_FILE),
            beam=beam_width, batch_size=batch_size,
        )

        self.beam_width = beam_width
        self._rescore = rescore
        if self._rescore:
            # loading the word_prob_dict for rescoring module
            dicts_folder = os.path.join(models_path, DICTS_FOLDER)
            self.word_prob_dicts = {}
            for la in tqdm.tqdm(self.tgt_langs, desc="Loading dicts into RAM"):
                self.word_prob_dicts[la] = ujson.load(open(
                    os.path.join(dicts_folder, DICT_FILE_FORMAT % la)
                ))

    def download_models(self, models_path, download_url):
        '''
        Download models from bucket
        '''
        # added by yash
        model_file_path = os.path.join(models_path, MODEL_FILE)
        if not os.path.isfile(model_file_path):
            print('Downloading Multilingual model for transliteration')
            remote_url = download_url
            downloaded_zip_path = os.path.join(models_path, 'model.zip')

            dload(url=remote_url, save_to_path=downloaded_zip_path, max_time=None)

            if not os.path.isfile(downloaded_zip_path):
                exit(f'ERROR: Unable to download model from {
                     remote_url} into {models_path}')

            with zipfile.ZipFile(downloaded_zip_path, 'r') as zip_ref:
                zip_ref.extractall(models_path)

            if os.path.isfile(model_file_path):
                os.remove(downloaded_zip_path)
            else:
                exit(f'ERROR: Unable to find models in {
                     models_path} after download')

            print("Models downloaded to:", models_path)
            print(
                "NOTE: When uninstalling this library, REMEMBER to delete the models manually")
        return model_file_path

    def download_dicts(self, models_path, download_url):
        '''
        Download language model probablitites dictionaries
        '''
        dicts_folder = os.path.join(models_path, DICTS_FOLDER)
        if not os.path.isdir(dicts_folder):
            # added by yash
            print(
                'Downloading language model probablitites dictionaries for rescoring module')
            remote_url = download_url
            downloaded_zip_path = os.path.join(models_path, 'dicts.zip')

            dload(url=remote_url, save_to_path=downloaded_zip_path, max_time=None)

            if not os.path.isfile(downloaded_zip_path):
                exit(f'ERROR: Unable to download model from {
                     remote_url} into {models_path}')

            with zipfile.ZipFile(downloaded_zip_path, 'r') as zip_ref:
                zip_ref.extractall(models_path)

            if os.path.isdir(dicts_folder):
                os.remove(downloaded_zip_path)
            else:
                exit(f'ERROR: Unable to find models in {
                     models_path} after download')
        return dicts_folder

    def indic_normalize(self, words, lang_code):
        if lang_code not in ['gom', 'ks', 'ur', 'mai', 'brx', 'mni']:
            normalizer = normalizer_factory.get_normalizer(lang_code)
            words = [normalizer.normalize(word) for word in words]

        if lang_code in ['mai', 'brx']:
            normalizer = normalizer_factory.get_normalizer('hi')
            words = [normalizer.normalize(word) for word in words]

        if lang_code in ['ur']:
            words = [shahmukhi_normalize(word) for word in words]

        if lang_code == 'gom':
            normalizer = normalizer_factory.get_normalizer('kK')
            words = [normalizer.normalize(word) for word in words]

        # normalize and tokenize the words
        # words = self.normalize(words)

        # manully mapping certain characters
        # words = self.hard_normalizer(words)
        return words

    def pre_process(self, words, src_lang, tgt_lang):
        # TODO: Move normalize outside to efficiently perform at sentence-level

        if src_lang != 'en':
            self.indic_normalize(words, src_lang)

        # convert the word into sentence which contains space separated chars
        words = [' '.join(list(word.lower())) for word in words]

        lang_code = tgt_lang if src_lang == 'en' else src_lang
        # adding language token
        words = ['__' + lang_code + '__ ' + word for word in words]

        return words

    def rescore(self, res_dict, result_dict, tgt_lang, alpha):

        alpha = alpha
        # word_prob_dict = {}
        word_prob_dict = self.word_prob_dicts[tgt_lang]

        candidate_word_prob_norm_dict = {}
        candidate_word_result_norm_dict = {}

        input_data = {}
        for i in res_dict.keys():
            input_data[res_dict[i]['S']] = []
            for j in range(len(res_dict[i]['H'])):
                input_data[res_dict[i]['S']].append(res_dict[i]['H'][j][0])

        for src_word in input_data.keys():
            candidates = input_data[src_word]

            candidates = [' '.join(word.split(' ')) for word in candidates]

            total_score = 0

            if src_word.lower() in result_dict.keys():
                for candidate_word in candidates:
                    if candidate_word in result_dict[src_word.lower()].keys():
                        total_score += result_dict[src_word.lower()
                                                   ][candidate_word]

            candidate_word_result_norm_dict[src_word.lower()] = {}

            for candidate_word in candidates:
                candidate_word_result_norm_dict[src_word.lower()][candidate_word] = (
                    result_dict[src_word.lower()][candidate_word]/total_score)

            candidates = [''.join(word.split(' ')) for word in candidates]

            total_prob = 0

            for candidate_word in candidates:
                if candidate_word in word_prob_dict.keys():
                    total_prob += word_prob_dict[candidate_word]

            candidate_word_prob_norm_dict[src_word.lower()] = {}
            for candidate_word in candidates:
                if candidate_word in word_prob_dict.keys():
                    candidate_word_prob_norm_dict[src_word.lower()][candidate_word] = (
                        word_prob_dict[candidate_word]/total_prob)

        output_data = {}
        for src_word in input_data.keys():

            temp_candidates_tuple_list = []
            candidates = input_data[src_word]
            candidates = [''.join(word.split(' ')) for word in candidates]

            for candidate_word in candidates:
                if candidate_word in word_prob_dict.keys():
                    temp_candidates_tuple_list.append((candidate_word, alpha*candidate_word_result_norm_dict[src_word.lower(
                    )][' '.join(list(candidate_word))] + (1-alpha)*candidate_word_prob_norm_dict[src_word.lower()][candidate_word]))
                else:
                    temp_candidates_tuple_list.append((candidate_word, 0))

            temp_candidates_tuple_list.sort(key=lambda x: x[1], reverse=True)

            temp_candidates_list = []
            for cadidate_tuple in temp_candidates_tuple_list:
                temp_candidates_list.append(' '.join(list(cadidate_tuple[0])))

            output_data[src_word] = temp_candidates_list

        return output_data

    def post_process(self, translation_str, tgt_lang):
        lines = translation_str.split('\n')
        list_s, list_h = [], []
        res_dict, result_dict = {}, {}
        transliterated_word_list = []

        for line in lines:
            if 'S-' in line:
                list_s.append(line)
            elif 'H-' in line:
                list_h.append(line)

        list_s = sorted(list_s,
                        key=lambda x: int(x.split('\t')[0].split('-')[1]))
        list_h = sorted(list_h,
                        key=lambda x: int(x.split('\t')[0].split('-')[1]))

        for s in list_s:
            s_id = int(s.split('\t')[0].split('-')[1])

            res_dict[s_id] = {'S': s.split('\t')[1], 'H': [
                (h.split('\t')[2], pow(2, float(h.split('\t')[1]))) for h in list_h if int(h.split('\t')[0].split('-')[1]) == s_id]}
            res_dict[s_id]['H'].sort(key=lambda x: float(x[1]), reverse=True)

        for s_id, res in res_dict.items():
            result_dict[res['S']] = dict(res['H'])
            transliterated_word_list += res['H']

        if self._rescore:
            output_dir = self.rescore(
                res_dict, result_dict, tgt_lang, alpha=0.9)

            transliterated_word_list = [output_dir[v['S']][0]
                                        for k, v in res_dict.items()]

        else:
            transliterated_word_list = [item[0]
                                        for item in transliterated_word_list]

        transliterated_word_list = [
            ''.join(word.split(' ')) for word in transliterated_word_list]

        return transliterated_word_list

    def _transliterate_word(self, text, src_lang, tgt_lang, topk=4, nativize_punctuations=True, nativize_numerals=False):
        if not text:
            return text
        text = text.lower().strip()

        if src_lang != 'en':
            # Our model does not transliterate native punctuations or numerals
            # So process them first so that they are not considered for transliteration
            text = text.translate(INDIC_TO_LATIN_PUNCT_TRANSLATOR)
            text = text.translate(INDIC_TO_STANDARD_NUMERALS_TRANSLATOR)
        else:
            # Transliterate punctuations & numerals if tgt_lang is Indic
            if nativize_punctuations:
                if tgt_lang in RTL_LANG_CODES:
                    text = text.translate(LATIN_TO_PERSOARABIC_PUNC_TRANSLATOR)
                text = nativize_latin_fullstop(text, tgt_lang)
            if nativize_numerals:
                text = text.translate(
                    LATIN_TO_NATIVE_NUMERALS_TRANSLATORS[tgt_lang])

        matches = LANG_WORD_REGEXES[src_lang].findall(text)

        if not matches:
            return [text]

        src_word = matches[-1]

        transliteration_list = self.batch_transliterate_words(
            [src_word], src_lang, tgt_lang, topk=topk)

        if tgt_lang != 'en' or tgt_lang != 'sa':
            # If users want to avoid yuktAkshara, this is facilitated by allowing them to type subwords inorder to construct a word
            # For example, "ଜନ୍‍ସନ୍‍ଙ୍କୁ" can be written by "ଜନ୍‍" + "ସନ୍‍" + "କୁ"
            # Not enabled for Sanskrit, as sandhi compounds are generally written word-by-word
            for i in range(len(transliteration_list)):
                transliteration_list[i] = hardfix_wordfinal_virama(
                    transliteration_list[i])

        if src_word == text:
            return transliteration_list

        return [
            rreplace(text, src_word, tgt_word)
            for tgt_word in transliteration_list
        ]

    def batch_transliterate_words(self, words, src_lang, tgt_lang, topk=4):
        print('Processing words: ', len(words))
        perprcossed_words = self.pre_process(words, src_lang, tgt_lang)
        translation_str = self.transliterator.translate(
            perprcossed_words, nbest=topk)

        transliteration_list = self.post_process(translation_str, tgt_lang)

        # Lang-specific patches. TODO: Move to indic-nlp-library
        if tgt_lang == 'mr':
            for i in range(len(transliteration_list)):
                transliteration_list[i] = transliteration_list[i].replace(
                    "अॅ", 'ॲ')

        if tgt_lang == 'or':
            for i in range(len(transliteration_list)):
                transliteration_list[i] = fix_odia_confusing_ambiguous_yuktakshara(
                    transliteration_list[i])

        if tgt_lang == 'sa':
            for i in range(len(transliteration_list)):
                transliteration_list[i] = explicit_devanagari_wordfinal_schwa_delete(
                    words[0], transliteration_list[i])
            # Retain only unique, preserving order
            transliteration_list = list(dict.fromkeys(transliteration_list))

        return transliteration_list

    def _transliterate_sentence(self, text, src_lang, tgt_lang, nativize_punctuations=True, nativize_numerals=False):
        # TODO: Minimize code redundancy with `_transliterate_word()`

        if not text:
            return text
        text = text.lower().strip()

        if src_lang != 'en':
            # Our model does not transliterate native punctuations or numerals
            # So process them first so that they are not considered for transliteration
            text = text.translate(INDIC_TO_LATIN_PUNCT_TRANSLATOR)
            text = text.translate(INDIC_TO_STANDARD_NUMERALS_TRANSLATOR)
        else:
            # Transliterate punctuations & numerals if tgt_lang is Indic
            if nativize_punctuations:
                if tgt_lang in RTL_LANG_CODES:
                    text = text.translate(LATIN_TO_PERSOARABIC_PUNC_TRANSLATOR)
                text = nativize_latin_fullstop(text, tgt_lang)
            if nativize_numerals:
                text = text.translate(
                    LATIN_TO_NATIVE_NUMERALS_TRANSLATORS[tgt_lang])

        matches = LANG_WORD_REGEXES[src_lang].findall(text)

        if not matches:
            return text

        out_str = text

        global tranliterated_words_map
        matches_copy = []
        # if match in tranliterated_words_map.keys(): then remove it from matches
        for word in matches_copy:
            if word not in tranliterated_words_map:
                matches_copy.append(word)

        if len(matches_copy) > 0:
            transliterated_words = self.batch_transliterate_words(
                matches_copy, src_lang, tgt_lang)
            assert (len(matches) == len(transliterated_words))

            # updated transliterated_words_map global variable
            for match, tgt_word in zip(matches_copy, transliterated_words):
                tranliterated_words_map[match] = tgt_word
            print('\n\nLength of transliterated_words_map is: ',
                  len(tranliterated_words_map))

            with open(cache_file_path, 'w') as f:
                json.dump(tranliterated_words_map, f, indent=4)

        # for match, tgt_word in zip(matches, transliterated_words):
        #     out_str = re.sub(match, tgt_word, out_str, 1)

        for match in matches:
            out_str = out_str.replace(match, tranliterated_words_map[match], 1)

        return out_str
