#!/usr/bin/env python
import argparse
import random
import math
import os
import errno
import sys
import boto3
import botocore
import shutil
import gzip
import logging as log
from Queue import Queue
from threading import Thread

try:
  import speedtile_pb2
except ImportError:
  print 'You need to generate protobuffer source via: protoc --python_out . --proto_path ../proto ../proto/*.proto'
  sys.exit(1)

###############################################################################
#world bb
minx_ = -180
miny_ = -90
maxx_ = 180
maxy_ = 90

def get_tile_count(filename):
  #lets load the protobuf speed tile
  spdtile = speedtile_pb2.SpeedTile()
  with open(filename, 'rb') as f:
    spdtile.ParseFromString(f.read())
  if spdtile.subtiles[0].totalSegments <= spdtile.subtiles[0].subtileSegments:
    return 0
  return int(math.ceil(spdtile.subtiles[0].totalSegments / (spdtile.subtiles[0].subtileSegments * 1.0)))

###############################################################################
### tile 2415
### segment0{refspdlist[hour0:avgspd, hour1:avgspd, hour2:avgspd, etc]} -> sort lo to hi, index in to get refspd20:int, refspd40:int, refspd60:int, refspd80:int
### segment1{refspdlist[hour0:avgspd, hour1:avgspd, hour2:avgspd, etc]}
# get the avg speeds for a given segment for each hour (168 hr/day) over 52 weeks

def createAvgSpeedList(fileNames):
  #each segment has its own list of speeds, we dont know how many segments for the avg speed list to start with
  segments = []
  minSeconds = None
  maxSeconds = None

  #need to loop thru all of the speed tiles for a given tile id
  for fileName in fileNames:
    #lets load the protobuf speed tile
    spdtile = speedtile_pb2.SpeedTile()
    with open(fileName, 'rb') as f:
      spdtile.ParseFromString(f.read())

    log.debug('Process ' + fileName)
    subtileCount = 1;
    #we now need to retrieve all of the speeds for each segment in this tile
    for subtile in spdtile.subtiles:
      log.debug('>>>>> subtileCount per file=' + str(subtileCount))
      subtileCount += 1
      if minSeconds is None or minSeconds > subtile.rangeStart:
        minSeconds = subtile.rangeStart
      if maxSeconds is None or maxSeconds < subtile.rangeEnd:
        maxSeconds = subtile.rangeEnd

      #make sure that there are enough lists in the list for all segments
      missing = spdtile.subtiles[0].totalSegments - len(segments)
      log.debug('spdtile.subtiles[0].totalSegments=' + str(spdtile.subtiles[0].totalSegments) + ' | len(segments)=' + str(len(segments)) + ' | missing=' + str(missing))
      if missing > 0:
        segments.extend([ [] for i in range(0, missing) ])
      
      print 'total # created in segments ' + str(subtile.totalSegments)
      entries = subtile.unitSize / subtile.entrySize
      print '# of entries per segment : ' + str(entries)
      for i, speed in enumerate(subtile.speeds):
        segmentIndex = subtile.startSegmentIndex + int(math.floor(i/entries))
        log.debug('SPEED: i=' + str(i) + ' | segmentIndex=' + str(segmentIndex) + ' | segmentId=' + str((segmentIndex<<25)|(subtile.index<<3)|subtile.level) + ' | speed=' + str(speed))
        if speed > 0 and speed <= 160:
          segments[segmentIndex].append(speed)
        elif speed > 160:
          log.error('INVALID SPEED: i=' + str(i) + ' | segmentIndex=' + str(segmentIndex) + ' | segmentId=' + str((segmentIndex<<25)|(subtile.index<<3)|subtile.level) + ' | speed=' + str(speed))

  #sort each list of speeds per segment
  for i, segment in enumerate(segments):
    if len(segment) > 0:
      print '# of valid average speeds in segment ' + str(i) + ' is ' + str(len(segment))
    segment.sort()
    log.debug('SORTED SPEEDS: segmentIndex=' + str(i) + ' | speeds=' + str(segment))

  return segments, minSeconds, maxSeconds

###############################################################################
def createRefSpeedTile(path, fileName, speedListPerSegment, level, index, minSeconds, maxSeconds):
  log.debug('createRefSpeedTiles ###############################################################################')

  tile = speedtile_pb2.SpeedTile()
  st = tile.subtiles.add()
  st.level = level
  st.index = index
  st.startSegmentIndex = 0
  st.totalSegments = len(speedListPerSegment)
  st.subtileSegments = len(speedListPerSegment)
  #time stuff
  st.rangeStart = minSeconds
  st.rangeEnd = maxSeconds
  st.unitSize = st.rangeEnd - st.rangeStart
  st.entrySize = st.unitSize
  st.description = 'Reference speeds over 1 year from 08.2016 through 07.2017' #TODO: get this from range start and end

  print 'speedListPerSegment length: ' + str(len(speedListPerSegment))
  #bucketize avg speeds into 20%, 40%, 60% and 80% reference speed buckets
  #for each segment
  for segment in speedListPerSegment:
    size = len(segment)
    st.referenceSpeeds20.append(segment[int(size * .2)] if size > 0 else 0) #0 here represents no data
    st.referenceSpeeds40.append(segment[int(size * .4)] if size > 0 else 0) #0 here represents no data
    st.referenceSpeeds60.append(segment[int(size * .6)] if size > 0 else 0) #0 here represents no data
    st.referenceSpeeds80.append(segment[int(size * .8)] if size > 0 else 0) #0 here represents no data

  #print str(st.referenceSpeeds20)
  #print str(st.referenceSpeeds40)
  #print str(st.referenceSpeeds60)
  #print str(st.referenceSpeeds80)

  #write it out
  with open(path + fileName, 'ab') as f:
    f.write(tile.SerializeToString())

class BoundingBox(object):

  def __init__(self, min_x, min_y, max_x, max_y):
     self.minx = min_x
     self.miny = min_y
     self.maxx = max_x
     self.maxy = max_y

class TileHierarchy(object):

  def __init__(self):
    self.levels = {}
    # local
    self.levels[2] = Tiles(BoundingBox(minx_,miny_,maxx_,maxy_),.25)
    # arterial
    self.levels[1] = Tiles(BoundingBox(minx_,miny_,maxx_,maxy_),1)
    # highway
    self.levels[0] = Tiles(BoundingBox(minx_,miny_,maxx_,maxy_),4)

class Tiles(object):

  def __init__(self, bbox, size):
     self.bbox = bbox
     self.tilesize = size

     self.ncolumns = int(math.ceil((self.bbox.maxx - self.bbox.minx) / self.tilesize))
     self.nrows = int(math.ceil((self.bbox.maxy - self.bbox.miny) / self.tilesize))
     self.max_tile_id = ((self.ncolumns * self.nrows) - 1)

  def Digits(self, number):
    digits = 1 if (number < 0) else 0
    while long(number):
       number /= 10
       digits += 1
    return long(digits)

  # get the File based on tile_id and level
  def GetFile(self, tile_id, level):

    max_length = self.Digits(self.max_tile_id)

    remainder = max_length % 3
    if remainder:
       max_length += 3 - remainder

    #if it starts with a zero the pow trick doesn't work
    if level == 0:
      file_suffix = '{:,}'.format(int(pow(10, max_length)) + tile_id).replace(',', '/')
      file_suffix += "."
      file_suffix += "spd"
      file_suffix = "0" + file_suffix[1:]
      return file_suffix

    #it was something else
    file_suffix = '{:,}'.format(level * int(pow(10, max_length)) + tile_id).replace(',', '/')
    file_suffix += "."
    file_suffix += "spd"
    return file_suffix

#this is from:
#http://code.activestate.com/recipes/577187-python-thread-pool/

class Worker(Thread):
    """Thread executing tasks from a given tasks queue"""
    def __init__(self, tasks):
        Thread.__init__(self)
        self.tasks = tasks
        self.daemon = True
        self.start()

    def run(self):
        while True:
            func, args, kargs = self.tasks.get()
            try: func(*args, **kargs)
            except Exception, e: print e
            self.tasks.task_done()

class ThreadPool:
    """Pool of threads consuming tasks from a queue"""
    def __init__(self, num_threads):
        self.tasks = Queue(num_threads)
        for _ in range(num_threads): Worker(self.tasks)

    def add_task(self, func, *args, **kargs):
        """Add a task to the queue"""
        self.tasks.put((func, args, kargs))

    def wait_completion(self):
        """Wait for completion of all the tasks in the queue"""
        self.tasks.join()

###############################################################################

# Read in protobuf files from the datastore output in AWS to read in the lengths, speeds & next segment ids and generate the segment speed files in proto output format
if __name__ == "__main__":
  parser = argparse.ArgumentParser(description='Generate speed tiles', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument('--speedtile-list', type=str, nargs='+', help='A list of the PDE speed tiles containing the average speeds per segment per hour of day for one week')
  parser.add_argument('--ref-tile-path', type=str, help='The public data extract speed tile containing the average speeds per segment per hour of day for one week', required=True)
  parser.add_argument('--bucket', type=str, help='AWS bucket location', required=True)
  parser.add_argument('--year', type=str, help='The year you wish to get', required=True)
  parser.add_argument('--level', type=int, help='The level to target', required=True)
  parser.add_argument('--tile-id', type=int, help='The tile id to target', required=True)
  parser.add_argument('--no-separate-subtiles', help='If present all subtiles will be in the same tile', action='store_true')
  parser.add_argument('--local', help='Enable local file processing', action='store_true')
  parser.add_argument('--verbose', '-v', help='Turn on verbose output i.e. DEBUG level logging', action='store_true')

  # parse the arguments
  args = parser.parse_args()

  # setup log
  if args.verbose:
    log.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout, level=log.DEBUG)
    log.debug('ref-tile-path=' + args.ref_tile_path)
    log.debug('bucket=' + args.bucket)
    log.debug('year=' + args.year)
    log.debug('level=' + str(args.level))
    log.debug('tile-id=' + str(args.tile_id))
  else:
    log.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)

  ################################################################################
  # download the speed tiles from aws and decompress
  spdFileNames = []
  if not args.local:

    # work function for our threads.  Downloads and decompresses the speed tile
    def work(bucket, directory, filename):
      s3 = boto3.client('s3')
      try:
        s3.download_file(bucket, filename + ".gz", directory + filename + ".gz")
      except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == "404":
          print "File not found in bucket! " + filename + ".gz"
          raise
      decompressedFile = gzip.GzipFile(directory + filename + ".gz", mode='rb')
      with open(directory + filename, 'w') as outfile:
        outfile.write(decompressedFile.read())
      print('[INFO] downloaded and decompressed file: ' + filename + '.gz from s3 bucket: ' + bucket)

    #our work dir.  deleted everytime if exists
    directory = "ref_working_dir/"
    shutil.rmtree(directory, ignore_errors=True)
    tile_hierarchy = TileHierarchy()
    key_prefix = args.year + "/"

    s3 = boto3.client('s3')

    #get the first tile *.0.gz so that we can determine the number of subtiles we have
    # if they are separated into subtiles
    print('[INFO] starting download from s3 bucket: ' + args.bucket)
    weeks_per_year = 52
    week = 0
    # for every week in the year
    while ( week < weeks_per_year):
      key = key_prefix + str(week) + "/"
      file_name = tile_hierarchy.levels[args.level].GetFile(args.tile_id, args.level)
      key += file_name
      try:
        file_path = os.path.dirname(key + ".0.gz" )
        # create the directory for the tiles
        if not os.path.exists(directory + file_path):
          try:
            os.makedirs(directory + file_path)
          except OSError as e:
            if e.errno != errno.EEXIST:
              raise
        #download the file
        s3.download_file(args.bucket, key + ".0.gz", (directory + key + ".0.gz"))
      except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] != "404":
          raise
        else:
          week += 1
          continue
      print('[INFO] downloaded and decompressed file: ' + (key + ".0.gz") + ' from s3 bucket: ' + args.bucket)
      #decompress the file
      decompressedFile = gzip.GzipFile(directory + key + ".0.gz", mode='rb')
      with open(directory + key + ".0", 'w') as outfile:
        outfile.write(decompressedFile.read())

      #append the file name
      spdFileNames.append(os.path.abspath(outfile.name))

      #if everything is in one tile, then we are done.
      if not args.no_separate_subtiles:
        subtile_suffix = get_tile_count(directory + key + ".0")

        # create a thread pool based on the subtile count, download, and decompress them
        if subtile_suffix != 0:
          p=ThreadPool(subtile_suffix)
          i = 1;
          while ( i < subtile_suffix):
            p.add_task(work, args.bucket, directory, key + "." + str(i))
            spdFileNames.append(os.path.abspath(directory + key + "." + str(i)))
            i += 1
          p.wait_completion()

      week += 1

  ################################################################################

  print 'getting avg speeds from list of protobuf speed tile extracts'
  ref_tile_file = None
  if not args.local:
    log.debug('AWS speed processing...')
    speedListPerSegment, minSeconds, maxSeconds = createAvgSpeedList(spdFileNames)
    ref_tile_file = os.path.splitext(os.path.splitext(os.path.basename(spdFileNames[0]))[0])[0]
    ref_tile_file += ".ref"
  else:
    log.debug('LOCAL speed processing...')
    speedListPerSegment, minSeconds, maxSeconds = createAvgSpeedList(args.speedtile_list)
    ref_tile_file = os.path.splitext(os.path.splitext(os.path.basename(args.speedtile_list[0]))[0])[0]
    ref_tile_file += ".ref"

  if args.verbose:
    print("Ref output filename: " + ref_tile_file)

  print 'create reference speed tiles for each segment'
  createRefSpeedTile(args.ref_tile_path, ref_tile_file, speedListPerSegment, args.level, args.tile_id, minSeconds, maxSeconds)

  print 'done'

