import os
import operator
import numpy as np
import cv2
from tqdm import tqdm
from lib.cli import FullPaths
from lib.cli import DirectoryProcessor

class SortProcessor(DirectoryProcessor):

    def __init__(self, subparser, command, description='default'):
        self.parse_arguments(description, subparser, command)
        
    def process_arguments(self, arguments):
        self.arguments = arguments
        print( self.arguments.input_dir )

        self.process()

    def parse_arguments(self, description, subparser, command):
        parser = subparser.add_parser(
            command,
            help="Sort face images in aligned folder by selected method.",
            description=description,
            epilog="Questions and feedback: \
            https://github.com/deepfakes/faceswap-playground"
        )

        parser.add_argument('-i', '--input',
                            action=FullPaths,
                            dest="input_dir",
                            default="input_dir",
                            help="Input directory of aligned faces. ")
                             
        parser.add_argument('-by', '--by',
                            type=str,
                            choices=("blur", "similarity"), # case sensitive because this is used to load a plugin.
                            dest='method',
                            default="similarity",
                            help="Sort by method.")
                            
                            
        parser = self.add_optional_arguments(parser)
        parser.set_defaults(func=self.process_arguments)

    def add_optional_arguments(self, parser):        
        return parser

    def process(self):        
        if self.arguments.method.lower() == 'blur':
            self.process_blur()
        elif self.arguments.method.lower() == 'similarity':
            self.process_similarity()
            
    def process_blur(self):
        input_dir = self.arguments.input_dir
        
        print ("Sorting by blur...")         
        img_list = [ [x, self.estimate_blur(cv2.imread(x))] for x in tqdm(self.find_images(input_dir), desc="Loading") ]
        print ("Sorting...")    
        img_list = sorted(img_list, key=operator.itemgetter(1), reverse=True) 
        self.process_final_rename(input_dir, img_list)        
        print ("Done.")
  
    def process_similarity(self):
        input_dir = self.arguments.input_dir
        
        print ("Sorting by similarity...")
        
        img_list = [ [x, cv2.calcHist([cv2.imread(x)], [0], None, [256], [0, 256]) ] for x in tqdm( self.find_images(input_dir), desc="Loading") ]

        img_list_len = len(img_list)
        for i in tqdm ( range(0, img_list_len-1), desc="Sorting"):
            min_score = 9999.9
            j_min_score = i+1
            for j in range(i+1,len(img_list)):
                score = cv2.compareHist(img_list[i][1], img_list[j][1], cv2.HISTCMP_BHATTACHARYYA)
                if score < min_score:
                    min_score = score
                    j_min_score = j            
            img_list[i+1], img_list[j_min_score] = img_list[j_min_score], img_list[i+1]
            
        self.process_final_rename (input_dir, img_list)
                
        print ("Done.")
        
    def process_final_rename(self, input_dir, img_list):
        for i in tqdm( range(0,len(img_list)), desc="Renaming" , leave=False):
            src = img_list[i][0]
            src_basename = os.path.basename(src)       

            dst = os.path.join (input_dir, '%.5d_%s' % (i, src_basename ) )
            try:
                os.rename (src, dst)
            except:
                print ('fail to rename %s' % (src) )    
                
        for i in tqdm( range(0,len(img_list)) , desc="Renaming" ):
            src = img_list[i][0]
            src_basename = os.path.basename(src)
            
            src = os.path.join (input_dir, '%.5d_%s' % (i, src_basename) )
            dst = os.path.join (input_dir, '%.5d%s' % (i, os.path.splitext(src_basename)[1] ) )
            try:
                os.rename (src, dst)
            except:
                print ('fail to rename %s' % (src) )
                
    def find_images(self, input_dir):
        result = []
        extensions = [".jpg", ".png", ".jpeg"]
        for root, dirs, files in os.walk(input_dir):
            for file in files:
                if os.path.splitext(file)[1].lower() in extensions:
                    result.append (os.path.join(root, file))
        return result

    def estimate_blur(self, image):
        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        blur_map = cv2.Laplacian(image, cv2.CV_64F)
        score = np.var(blur_map)
        return score