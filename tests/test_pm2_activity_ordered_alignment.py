import copy
import hashlib
import json
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from PIL import Image
from pm2_animation_lab.pm2_activity_delivery_check import DeliveryError
from pm2_animation_lab.pm2_activity_ordered_alignment import align
from pm2_animation_lab.pm2_activity_pipeline import bind,compile_sequence
from pm2_animation_lab.pm2_activity_timed_comparison import verify_recording
from pm2_animation_lab.pm2_activity_condition_search import execute_day
from pm2_animation_lab.pm2_lbx_pt1 import DecodedPattern
from pm2_animation_lab import pm2_activity_pipeline as pipeline
from pm2_animation_lab import pm2_activity_timeline as tl
def picture(n):return Image.new('RGB',(2,2),(n,n,n))
def run(n,a,z):return {'rgb_sha256':hashlib.sha256(picture(n).tobytes()).hexdigest(),'first_frame':a,'end_frame_exclusive':z}


class AlignmentTests(unittest.TestCase):
    def test_hidden_ticks_and_repeated_poses_keep_forward_order(self):
        images=[picture(n) for n in (0,1,1,2,1,1,1)]
        result=align(images,[run(0,10,15),run(1,15,22),run(2,22,25),run(1,25,50)],10,50,include_opening=True)
        self.assertEqual([g['ticks'] for g in result['groups']],[[0,1],[2],[3,4,5]])
        self.assertEqual(result['opening_end_frame'],15)
        self.assertEqual(result['groups'][-1]['end_frame_exclusive'],50)

    def test_native_reordering_cannot_be_retrieved_from_source_bank(self):
        with self.assertRaisesRegex(DeliveryError,'pixels_differ'):
            align([picture(1),picture(2),picture(3)],[run(1,0,3),run(3,3,6),run(2,6,9)],0,9)

    def test_missing_native_pose_fails_instead_of_dropping_source_tick(self):
        with self.assertRaisesRegex(DeliveryError,'pose_count'):
            align([picture(1),picture(2)],[run(1,0,3)],0,3)

    def test_copy_refresh_remains_inside_outgoing_interval(self):
        result=align([picture(1),picture(2)],[run(1,0,3),run(100,3,4),run(2,4,7)],0,7)
        self.assertEqual(result['groups'][0]['end_frame_exclusive'],4)
        self.assertNotIn('equality_status',result)  # Copy still needs publication proof.

    def test_selection_gaps_and_truncation_rejected(self):
        for runs in ([run(1,0,2),run(2,3,5)],[run(1,0,2),run(2,2,4)]):
            with self.assertRaisesRegex(DeliveryError,'full_native_runs'):
                align([picture(1),picture(2)],runs,0,5)

    def test_initial_transition_requires_evidence(self):
        with self.assertRaisesRegex(DeliveryError,'initial_transition'):
            align([picture(1)],[run(100,0,1),run(1,1,4)],0,4)

    def test_identical_opening_sunday_preserves_each_tick(self):
        result=align([picture(0)]*6+[picture(1)],[run(0,0,20),run(1,20,24)],0,24,include_opening=True)
        self.assertTrue(result['opening_merged_with_first_sunday'])
        self.assertEqual(result['groups'][0]['ticks'],list(range(5)))


class OpeningCompositionTests(unittest.TestCase):
    def render(self,scene,include_opening=True):
        # Original miniature asset fixture: 16px background and a partly
        # transparent foreground at authored byte column 1. Real mask blit.
        bg=SimpleNamespace(width_bytes=2,height_pixels=1)
        front=DecodedPattern(width_bytes=1,height_pixels=1,author_x_raw=1,author_y_raw=0,
            mask_bits=bytes([1,0,0,0,0,0,0,0]),color_indices=bytes([0]+[3]*7))
        with patch.object(pipeline,'bound',return_value=Path('fixture.zip')), \
             patch.object(pipeline,'read_lbx_asset_from_zip',return_value=(b'',{'entry_sha256':'a'*64})), \
             patch.object(pipeline,'parse_pt1',side_effect=[SimpleNamespace(records=[bg]),SimpleNamespace(records=[bg,bg]),SimpleNamespace(records=[bg,bg])]), \
             patch.object(pipeline,'decode_record_planes',return_value=b''), \
             patch.object(pipeline,'planes_to_indices',return_value=bytes([1]*16)), \
             patch.object(pipeline,'decode_mask_body_pair',return_value=front), \
             patch.object(pipeline,'read_bound',return_value={'entries':[[i,i,i] for i in range(16)]}):
            images,_=pipeline.compose({'calls':[]},{'scene':scene,'archive':{'sha256':'a'*64},'palette':{}},Path('.'),include_opening=include_opening)
        return images

    def test_forest_opening_includes_mask_and_authored_offset(self):
        for scene in ('JOB006','JOB009'):
            with self.subTest(scene=scene):
                pixels=self.render(scene)[0].tobytes()
                self.assertEqual(pixels[:27],bytes([1]*27))
                self.assertEqual(pixels[27:],bytes([3]*21))

    def test_plain_opening_and_tick_only_contract_are_preserved(self):
        self.assertEqual(self.render('JOB000')[0].tobytes(),bytes([1]*48))
        self.assertEqual(self.render('JOB009',False),[])


class LegacyRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.video=self.root/'recordings/live.mkv'
        config=self.root/'record.cfg';config.write_text('vcodec = "libx264rgb"\nvideo_qp="0"\nframe_drop_ratio="1"')
        self.config=bind(config)
        self.spec={'isolation_directory':str(self.root),'inputs':{'record_config':self.config}}
        self.ob={};self.save_spec()
        self.command={'spec_sha256':self.ob['capture_spec']['sha256'],'argv':['retroarch','-r',str(self.video),'--recordconfig',str(config)]}
        self.save_command();self.ob['recording_config']=self.config

    def save_spec(self):
        p=self.root/'spec.json';p.write_text(json.dumps(self.spec));self.ob['capture_spec']=bind(p)
    def save_command(self):
        p=self.root/'command.json';p.write_text(json.dumps(self.command));self.ob['capture_command']=bind(p)

    def test_original_launch_links_video_spec_and_lossless_config(self):
        verify_recording(self.ob,self.video,self.root)

    def test_wrong_video_command_is_rejected(self):
        self.command['argv'][2]=str(self.root/'another.mkv');self.save_command()
        with self.assertRaisesRegex(DeliveryError,'command_mismatch'):verify_recording(self.ob,self.video,self.root)

    def test_edited_spec_cannot_borrow_original_launch(self):
        self.spec['purpose']='changed';self.save_spec()
        with self.assertRaisesRegex(DeliveryError,'spec_mismatch'):verify_recording(self.ob,self.video,self.root)

    def test_recording_config_must_be_actual_command_argument(self):
        self.command['argv'][-1]=str(self.root/'different.cfg');self.save_command()
        with self.assertRaisesRegex(DeliveryError,'command_mismatch'):verify_recording(self.ob,self.video,self.root)

    def test_conflicting_provenance_is_rejected(self):
        self.ob['capture_session']=self.ob['capture_spec']
        with self.assertRaisesRegex(DeliveryError,'one_capture'):verify_recording(self.ob,self.video,self.root)

    def test_lossy_configuration_is_rejected_even_with_valid_bindings(self):
        p=Path(self.config['path']);p.write_text('vcodec="libx264"\nvideo_qp="0"\nframe_drop_ratio="1"')
        self.config=bind(p);self.spec['inputs']['record_config']=self.config;self.save_spec()
        self.command['spec_sha256']=self.ob['capture_spec']['sha256'];self.save_command();self.ob['recording_config']=self.config
        with self.assertRaisesRegex(DeliveryError,'lossless'):verify_recording(self.ob,self.video,self.root)


SOURCE=Path(os.environ.get('PM2_SOURCE_ROOT','missing-external-source'))
@unittest.skipUnless((SOURCE/'KOSOTEXT/JOB009.TXT').exists(),'Fixed original source is locally unavailable')
class SourceSpecialCases(unittest.TestCase):
    def request(self):
        return {'schema':'pm2_activity_comparison_request/v1','scene':'JOB009',
            'initialization_random_values':[],
            'entry_state':{'mode':'fresh_init_with_overrides','provenance':'test explicit inputs','overrides':[]},
            'prelude':{'branch':'hunting_intro','expected_state':None,'tick_count':12,'random_values':[1]},
            'schedule':{'kind':'excerpt','start_weekday':6,'start_boundary':'first_present','end_boundary':'after_last_exposure',
                'days':[{'ordinal':0,'branch':'failure','expected_state':2,'random_values':[]},
                        {'ordinal':1,'branch':'sunday','expected_state':None,'random_values':[]}]}}

    def test_hunting_intro_preserves_daily_calendar_and_all_28_ticks(self):
        seq,_=compile_sequence(self.request(),SOURCE)
        self.assertEqual(len(seq['ticks']),28)
        self.assertEqual([c['branch'] for c in seq['explicit_inputs']['calls']],['hunting_intro','failure','sunday'])

    def test_short_intro_is_rejected(self):
        r=self.request();r['prelude']['tick_count']=11
        with self.assertRaisesRegex(DeliveryError,'unsupported_source_prelude'):compile_sequence(r,SOURCE)

    def test_wrong_scene_cannot_use_hunting_intro(self):
        r=self.request();r['scene']='TRG004'
        with self.assertRaisesRegex(DeliveryError,'unsupported_source_prelude'):compile_sequence(r,SOURCE)

    def test_intro_does_not_consume_a_calendar_day(self):
        r=self.request();r['schedule']['days'][1].update(branch='failure',expected_state=2)
        with self.assertRaises(ValueError):compile_sequence(r,SOURCE)

    def test_natural_science_consumes_five_random_inputs_inside_daily_loop(self):
        source=tl.parse_source(SOURCE/'KOSOTEXT/TRG000.TXT','TRG000');state=tl._new_runtime(source)
        tl.RestrictedInterpreter(source,state,tl.RandomTape([])).execute_routine('ANIME_INIT')
        a,ta,_=execute_day(source,state,'success',[1]*5)
        b,tb,_=execute_day(source,state,'success',[2]*5)
        self.assertNotEqual(a.arrays['ALOCC'][5],b.arrays['ALOCC'][5])
        self.assertEqual(len(ta),len(tb))
        with self.assertRaises(tl.MissingInputError):execute_day(source,state,'success',[1]*4)


if __name__=='__main__':unittest.main()
