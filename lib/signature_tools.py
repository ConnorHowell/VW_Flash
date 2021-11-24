import logging
from Crypto.PublicKey import RSA
from pathlib import Path
from Crypto.Signature.pkcs1_15 import PKCS115_SigScheme
from Crypto.Hash import SHA256
from pprint import pprint

logger = logging.getLogger("VWFlash")

def sign_datablock(bin_file, private_key_path):
    with open(private_key_path, 'rb') as private_key_file:
        private_key = private_key_file.read()


    the_hash = SHA256.new(bin_file)
    pkcs115 = PKCS115_SigScheme(RSA.import_key(private_key))
    return pkcs115.sign(the_hash)    



def sign_bin(bin_file, private_key_path = None, boxcode = "", notes = ""):
    metadata = build_metadata(boxcode = boxcode, notes = notes)
    bin_file += metadata
    
    signature1 = sign_datablock(input_bin, "./data/VW_Flash.pub")
    if private_key_path:
        signature2 = sign_datablock(input_bin, private_key_path)
    else:
        signature2 = signature1
    
    signed_file = input_bin + signature1 + signature2

    return signed_file



def verify_bin(bin_file, signature, public_key_path):
    with open(public_key_path, 'rb') as public_key_file:
        public_key = public_key_file.read()


    the_hash = SHA256.new(bin_file)
    pkcs115 = PKCS115_SigScheme(RSA.import_key(public_key))

    try:
        verified = pkcs115.verify(the_hash, signature)
        return True
    except:
        return False

def build_metadata(boxcode = "", notes = ""):
    metadata = b'METADATA:' + boxcode[0:15].ljust(15,' ').encode("utf-8") + notes[0:70].ljust(70, ' ').encode("utf-8")
    return metadata

def read_bytes(file_path, public_key_file = None):
    bin_data = Path(file_path).read_bytes()

    #Check if there's metadata and signature(s) at the end of the file:
    sig_block = bin_data[-350:]
    if sig_block[0:9] == b'METADATA:':
        logger.warning("Found signature block in bin file, validating")
        #Print out the metadata that's included in the file
        logger.info(str(sig_block[0:-256]))

        #Pull the signatures out
        signature1 = sig_block[-256:-128]
        signature2 = sig_block[-128:]

        #Validate the first signature using the VW_Flash public key
        if verify_bin(bin_data[0:-256], signature1, "./data/VW_Flash.pub"):
            logger.warning("First signature validated")
        else:
            logger.critical("First signature failed!")

        #if the signatures are the same, there's no point checking the second one, just continue on
        if signature1 == signature2:
            logger.warning("No secondary signature found")

        elif public_key_file:
            if verify_bin(bin_data[0:-256], signature2, public_key_file):
                logger.warning("Second signature validated")
            else:
                logger.critical("Second signature failed!")

        else:
            logger.info("File contains additional signature, but no public key arg provided")

        #Pull the signature block off the end of the bin file so we can process it by itself
        bin_data = bin_data[0:-350]

    return bin_data

