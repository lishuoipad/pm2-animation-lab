"""Nonuniform timing, source-order preservation and equality-boundary tests."""
import copy
import unittest
from PIL import Image
from pm2_animation_lab.pm2_activity_clock import ClockError
from pm2_animation_lab.pm2_activity_delivery_check import DeliveryError, check_records
from pm2_animation_lab.pm2_activity_timed_comparison import calibrated_groups, source_frame_for_group, difference, copy_image
from pm2_animation_lab.pm2_activity_transition_copy import PlanarFit


class TimedComparisonTests(unittest.TestCase):
    def observation(self):
        return {'opening_end_frame':1, 'native_index':{'path':'bound-native','sha256':'0'*64},
                'groups':[{'ticks':[0], 'first_frame':1, 'end_frame_exclusive':2},
                          {'ticks':[1], 'first_frame':2, 'end_frame_exclusive':3}]}

    def index(self):
        return {'timebase':[1,1000], 'frames':[{'pts':v} for v in (1000,1500,1670,1953)]}

    def test_opening_and_daily_wait_keep_nonuniform_native_intervals(self):
        groups,_ = calibrated_groups([{},{}],self.observation(),self.index())
        self.assertEqual(groups,[{'ticks':[0],'start':[0,1],'end':[17,100]},
                                 {'ticks':[1],'start':[17,100],'end':[453,1000]}])

    def test_reordering_observed_source_ticks_is_rejected(self):
        obs=self.observation();obs['groups'][0]['ticks']=[1];obs['groups'][1]['ticks']=[0]
        with self.assertRaises(ClockError): calibrated_groups([{},{}],obs,self.index())

    def test_dropping_an_unmatched_tick_is_rejected(self):
        with self.assertRaises(ClockError): calibrated_groups([{},{},{}],self.observation(),self.index())

    def test_identical_sunday_ticks_can_share_a_visible_exposure(self):
        images=[Image.new('RGB',(2,2),'black') for _ in range(5)]
        self.assertEqual(source_frame_for_group(images,{'ticks':list(range(5))}).tobytes(),images[0].tobytes())

    def test_different_poses_cannot_be_hidden_in_a_sunday_hold(self):
        images=[Image.new('RGB',(2,2),'black'),Image.new('RGB',(2,2),'white')]
        with self.assertRaisesRegex(DeliveryError,'distinct_poses'):
            source_frame_for_group(images,{'ticks':[0,1]})

    def test_difference_counts_pixels_not_channels_and_keeps_bounding_box(self):
        actual=Image.new('RGB',(4,3),'black');synth=actual.copy()
        synth.putpixel((2,1),(255,255,255))
        count,box,mask=difference(actual,synth)
        self.assertEqual((count,box),(1,[2,1,3,2]));self.assertEqual(int(mask.sum()),1)

    def test_comparison_support_does_not_relax_strict_delivery_equality(self):
        oracle={'scene':'JOB004','source_sha256':'a','size':[2,2],'schedule':{},
            'groups':[{'ticks':[0],'start':[0,1],'end':[1,1],'rgb_sha256':'actual'}],
            'timing_tolerance':[0,1],'boundary_complete':True}
        candidate=copy.deepcopy(oracle);candidate['level']='activity'
        candidate['groups'][0]['rgb_sha256']='different'
        with self.assertRaisesRegex(DeliveryError,'ordered_rgb'):
            check_records(candidate,oracle)

    def test_copy_fit_rejects_non_byte_boundaries(self):
        im=Image.new('RGB',(320,128),'black')
        with self.assertRaisesRegex(DeliveryError,'outside_source_model'):
            copy_image(im,im,PlanarFit(40,2,7),[[i,i,i] for i in range(16)])

    def test_copy_fit_cannot_invent_color_planes(self):
        im=Image.new('RGB',(320,128),'black')
        with self.assertRaisesRegex(DeliveryError,'outside_source_model'):
            copy_image(im,im,PlanarFit(40,4,8),[[i,i,i] for i in range(16)])


if __name__=='__main__': unittest.main()
