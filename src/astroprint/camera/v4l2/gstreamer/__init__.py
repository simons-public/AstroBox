# coding=utf-8
__author__ = "AstroPrint Product Team <product@astroprint.com>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'

import logging
import time
import uuid

from threading import Event, Condition, Lock

from octoprint.events import eventManager, Events

from astroprint.camera.v4l2 import V4L2Manager
from astroprint.camera.v4l2.gstreamer.pipeline import AstroPrintPipeline
from astroprint.webrtc import webRtcManager

#
#  Camera Manager subclass for GStreamer
#

class GStreamerManager(V4L2Manager):
	name = 'gstreamer'

	def __init__(self):
		self._apPipeline = None
		self.pipeline = None
		self.cameraInfo = None
		self._openCameraCondition = Condition()
		self.eventManager = eventManager()

		self._logger = logging.getLogger(__name__)

		super(GStreamerManager, self).__init__()

		self._localPeers = []
		self._localFrame = None
		self._localPeersResponseWaiting = Lock()
		self.waitForPhoto = Event()

	@property
	def _gstreamerProcessRunning(self):
		return self._apPipeline and self._apPipeline.processRunning

	def _doOpenCamera(self):
		if self.number_of_video_device is not None:
			with self._openCameraCondition:
				if self._apPipeline is None:
					try:
						self._apPipeline = AstroPrintPipeline(
							'/dev/video%d' % self.number_of_video_device,
							self._settings['size'],
							self._settings['video_rotation'],
							self._settings['source'],
							self._settings['encoding'],
							self._onApPipelineFataError
						)
					except Exception as e:
						self._logger.error('Failed to open camera: %s' % e, exc_info= True)
						return False

				self._apPipeline.startProcess()

				return True
		else:
			return False

	def _doCloseCamera(self):
		if self._apPipeline:
			self._apPipeline.stopProcess()

		return True

	def isCameraOpened(self):
		return self._apPipeline and self._apPipeline.processRunning

	def _onApPipelineFataError(self):
		self._logger.error('AstroPrint Pipeline Fatal Error called')
		self._haltCamera()

	def _freeApPipeline(self):
		if self._apPipeline:
			self._apPipeline.stop()
			self._apPipeline = None

	def _haltCamera(self):
		self.close_camera()
		webRtcManager().closeAllSessions()

	def _doReScan(self):
		if super(GStreamerManager, self)._doReScan():
			self._logger.info("Found camera %s, encoding: %s and size: %s. Source used: %s" % (self.cameraInfo['name'], self._settings['encoding'] , self._settings['size'], self._settings['source']))
			self._freeApPipeline()

			return True

		return False


	def getNumberOfLocalPeers(self):
		return len(self._localPeers)

	def addLocalPeerReq(self):
		id = uuid.uuid4().hex

		self._localPeers.append(id)

		self._logger.debug('number of local peers: %d' % len(self._localPeers))

		if len(self._localPeers) == 1:
			self.start_local_video_stream()

		return id

	def removeLocalPeerReq(self,id):
		self._localPeers.remove(id)

		if len(self._localPeers) <= 0:
			self.stop_local_video_stream()
			self._logger.info('There are 0 local peers left')

	def removeAllLocalPeerReqs(self):
		self._localPeers = []
		self._logger.info('There are 0 local peers left')


	def getFrame(self,id):
		self.waitForPhoto.wait(3)
		if self.waitForPhoto.isSet():
			self.waitForPhoto.clear()
			if id in self._localPeers:
				return self._localFrame
		else:#auto set after time
			self.removeLocalPeerReq(id)
			self.eventManager.fire(Events.LOCAL_VIDEO_STREAMING_STOPPED,None)
			self.waitForPhoto.clear()
			return None

	def _responsePeersReq(self,photoData):
		self._localFrame = photoData

	def _onFrameTakenCallback(self,photoData):

		if photoData:

			if not self._localPeers:
				self.stop_local_video_stream()

			self._responsePeersReq(photoData)

			self.waitForPhoto.set()

	def start_local_video_stream(self):

		if self._cameraInactivity:
			self._cameraInactivity.lastActivity = time.time()

		if not self._gstreamerProcessRunning:
			if not self.open_camera():
				return

			self._apPipeline.startLocalVideo(self._onFrameTakenCallback)
			return

		self._apPipeline.startLocalVideo(self._onFrameTakenCallback)
		return

	def stop_local_video_stream(self):
		if self._apPipeline:
			self._apPipeline.stopLocalVideo()

	def localSessionAlive(self,id):
		return id in self._localPeers

	def _doStartVideoStream(self, doneCallback= None):
		if self.isVideoStreaming():
			if doneCallback:
				doneCallback(True)

		if not self._gstreamerProcessRunning:
			if not self.open_camera():
				if doneCallback:
					doneCallback(False)
				return

		self._apPipeline.startVideo(doneCallback)

	def _doStopVideoStream(self, doneCallback= None):
		if not self._gstreamerProcessRunning or not self.isVideoStreaming():
			if doneCallback:
				doneCallback(True)

		else:
			result = self._apPipeline.stopVideo()

			if doneCallback:
				doneCallback(result)


	def settingsChanged(self, cameraSettings):
		super(GStreamerManager, self).settingsChanged(cameraSettings)

		##When a change in settup is saved, the camera must be shouted down
		##(Janus included, of course)

		eventManager().fire(Events.GSTREAMER_EVENT, {
			'message': 'Your camera settings have been changed. Please reload to restart your video.'
		})
		##

		self._haltCamera()
		self.reScan()

	def _doGetPic(self, done, text=None):
		if self.isCameraConnected():
			if not self._gstreamerProcessRunning:
				if not self.open_camera():
					done(None)
					return

			self._apPipeline.takePhoto(done, text)
			return

		done(None)

	def shutdown(self):
		self._logger.info('Shutting Down GstreamerManager')
		self._freeApPipeline()
		self._haltCamera()
		webRtcManager().shutdown()

	def isVideoStreaming(self):
		if self._gstreamerProcessRunning:
			waitForDone = Event()
			respCont = [None]

			def onDone(isPlaying):
				if not waitForDone.is_set():
					respCont[0] = isPlaying

			self._apPipeline.isAnyVideoPlaying(onDone)


			waitForDone.wait(1.0)

			return respCont[0] is True

		else:
			return False

	def closeLocalVideoSession(self, sessionId):
		return webRtcManager().closeLocalSession(sessionId)

	@property
	def capabilities(self):
		return ['videoStreaming', 'videoformat-' + self._settings['encoding']]

	@property
	def _desiredSettings(self):
		return {
			'busSource': [
				{'value': 'USB', 'label': 'USB Camera'},
				{'value': 'raspicam', 'label': 'Raspicam'}
			],
			'frameSizes': [
				{'value': '640x480', 'label': 'Low (640 x 480)'},
				{'value': '1280x720', 'label': 'HD 720p (1280 x 720)'},
				{'value': '1920x1080', 'label': 'HD 1080p (1920 x 1080)'}
			],
			'cameraOutput': [
				{'value': 'x-raw', 'label': 'Raw Video'}
			],
			'fps': [],
			'videoEncoding': [
				{'value': 'h264', 'label': 'H.264'},
				{'value': 'vp8', 'label': 'VP8'}
			],
			'video_rotation': [
				{'value': '0', 'label': 'No Rotation'},
				{'value': '1', 'label': 'Rotate 90 degrees to the right'},
				{'value': '3', 'label': 'Rotate 90 degrees to the left'},
				{'value': '4', 'label': 'Flip horizontally'},
				{'value': '2', 'label': 'Flip vertically'}

			]
		}
