"""Focused VM semantics and fixed-source compatibility regressions."""
import random
import unittest
import numpy as np
from pm2_animation_lab import pm2_activity_timeline as t
from pm2_animation_lab import pm2_activity_batch_research as batch
from pm2_animation_lab.pm2_activity_native_diagnostic import nearest_pixels


def synthetic(lines, scalars=None, arrays=None):
    routine=t.Routine('CHECK',1,tuple(t.SourceLine(i+2,s) for i,s in enumerate(lines)))
    source=t.ParsedSource('JOB003','0'*64,'0'*64,(),{'CHECK':routine},{},{},{})
    state=t.RuntimeState(scalars or {},arrays or {},{})
    return source,state,routine


class VmTests(unittest.TestCase):
    def test_conjunction_disjunction_and_inactive_parent(self):
        source,state,_=synthetic([
            'IF ( A = 1 B = 2 ) X=8',
            'IF ( A = 9 )( B = 2 ) Y=7',
            'IF ( A = 0 )',
            '\tIF ( UNKNOWN = 9 ) Z=6'],{'A':1,'B':2,'X':0,'Y':0})
        t.RestrictedInterpreter(source,state,t.RandomTape([])).execute_routine('CHECK')
        self.assertEqual((state.scalars['X'],state.scalars['Y']),(8,7))
        self.assertNotIn('Z',state.scalars)
        state.scalars.update(A=2,X=0)
        t.RestrictedInterpreter(source,state,t.RandomTape([])).execute_routine('CHECK')
        self.assertEqual(state.scalars['X'],0)

    def test_random_return_register_and_inline_condition(self):
        source,state,_=synthetic(['RANDAM(3) IF (AX=2) X+=4'],{'X':1})
        tape=t.RandomTape([2]);t.RestrictedInterpreter(source,state,tape).execute_routine('CHECK')
        tape.require_exhausted()
        self.assertEqual((state.scalars['AX'],state.scalars['IRND'],state.scalars['X']),(2,2,5))

    def test_call_edges_execute_outside_the_tick_loop(self):
        source,state,routine=synthetic(['ALOCC[0]=ALOCC[3]','WWANIME(4,0)','X++',
            'LOOP TIMELOP','Y++','RET'],{'X':0,'Y':0,'SLCANM':0},{'ALOCC':[0,0,0,7]})
        tape=t.RandomTape([])
        t.execute_call_edge(source,state,tape,routine,'before')
        self.assertEqual(state.arrays['ALOCC'][0],7)
        self.assertEqual(state.scalars['X'],0)
        state.arrays['ALOCC'][0]=8
        t.execute_call_edge(source,state,tape,routine,'after')
        self.assertEqual(state.arrays['ALOCC'][0],8)
        self.assertEqual((state.scalars['X'],state.scalars['Y']),(0,1))

    def test_nearest_pixel_metric_keeps_real_error(self):
        actual=np.zeros((2,3,3),dtype=np.uint8)
        candidates=np.stack([actual.copy(),actual.copy()]);candidates[0,0,0]=[1,2,3]
        self.assertEqual(nearest_pixels(actual,candidates),(1,0))
        self.assertEqual(nearest_pixels(actual,candidates[:1]),(0,1))


@unittest.skipUnless((batch.SOURCE/'KOSOTEXT/JOB014.TXT').exists(),'fixed external source unavailable')
class FixedSourceTests(unittest.TestCase):
    def test_graveyard_event_consumes_its_mutable_loop_counter(self):
        seq,conditions=batch.scenario('JOB010','failure',1,20260907,include_graveyard_event=True)
        event=seq['calls'][-1]
        self.assertEqual(conditions['calls'][-1]['branch'],'graveyard_event')
        self.assertEqual(event['continuation_entry_state']['state']['scalars']['TIMELOP'],0)
        self.assertGreater(len(event['ticks']),0)
        self.assertLessEqual(len(event['ticks']),60)
        spec_calls=[dict(c) for c in conditions['calls']]
        spec_calls[-1]['tick_count']-=1
        with self.assertRaises(t.DeterminismError):
            t.build_timeline_sequence(batch.SOURCE/'KOSOTEXT/JOB010.TXT','JOB010',
                initialization_random_values=conditions['initialization_random_values'],
                entry_state=conditions['entry_state'],calls=spec_calls,source_commit=t.FIXED_SOURCE_COMMIT)

    def test_hunting_intro_is_twelve_ticks_and_preserves_state_into_day(self):
        seq,conditions=batch.scenario('JOB009','failure',1,20260907)
        self.assertEqual([c['branch'] for c in conditions['calls']],['hunting_intro','failure'])
        self.assertEqual([len(c['ticks']) for c in seq['calls']],[12,8])
        complete=seq['calls'][0]['continuation_entry_state']['state']
        self.assertEqual(complete['scalars']['FLAG_SHIKA'],1)
        self.assertEqual(t.canonical_sha256(complete),seq['calls'][1]['entry_complete_state_sha256'])
        projected=t.sequence_call_as_v1_timeline(seq,0)
        self.assertIn('ANMT_INTRO',projected['parser_scope']['routine_spans'])
        self.assertNotIn('ANIME_WIDW',projected['parser_scope']['routine_spans'])
        bad=dict(conditions['calls'][0]);bad['tick_count']=11
        with self.assertRaises(t.DeterminismError):
            t.build_timeline_sequence(batch.SOURCE/'KOSOTEXT/JOB009.TXT','JOB009',
                initialization_random_values=[],entry_state=conditions['entry_state'],
                calls=[bad],source_commit=t.FIXED_SOURCE_COMMIT)

    def test_sunday_advances_candles_without_advancing_the_class(self):
        sequence,conditions=batch.scenario('TRG006','success',2,20260907,0)
        self.assertEqual([c['branch'] for c in conditions['calls']],['sunday','success'])
        sunday=sequence['ticks'][0]['draw_operations'];monday=sequence['ticks'][5]['draw_operations']
        self.assertEqual([(d['track'],d['pattern_number']) for d in sunday],[(5,20),(6,22)])
        self.assertEqual([(d['track'],d['pattern_number']) for d in monday[:3]],[(5,21),(6,23),(7,24)])

    def initialize(self,scene):
        t.verify_source_checkout(batch.SOURCE,t.FIXED_SOURCE_COMMIT,scene)
        source=t.parse_source(batch.SOURCE/'KOSOTEXT'/(scene+'.TXT'),scene)
        state=t._new_runtime(source)
        interpreter=t.RestrictedInterpreter(source,state,batch.RecordedSyntheticTape(random.Random(17)))
        interpreter.execute_routine('ANIME_INIT')
        return source,state,interpreter

    def test_alias_is_adjacent_and_bounded(self):
        _,state,vm=self.initialize('JOB014');tail=state.arrays['FILM2'][2:]
        vm.execute_routine('WORKS1_TURN')
        self.assertEqual(state.arrays['ALOCCNT'][0],4)
        self.assertEqual(len(state.arrays['FILM0']),2)
        self.assertEqual(state.arrays['FILM2'][:2],[7,8])
        self.assertEqual(state.arrays['FILM2'][2:],tail)
        with self.assertRaises(t.UnsupportedSyntaxError):vm._set_array_value('FILM0',4,9,'CHECK',1,'test')

    def test_fixed_legacy_declarations_and_reserved_slots(self):
        _,state,_=self.initialize('TRG003')
        self.assertEqual(len(state.arrays['FILM0']),12)
        _,state,_=self.initialize('JOB011')
        self.assertEqual(len(state.arrays['FILM9']),10)
        self.assertEqual(state.arrays['ALOCCNT'][9],1)
        t._validate_runtime_call_boundary(state)
        state.arrays['ALOCCNT'][1]=1
        with self.assertRaises(t.DeterminismError):t._validate_runtime_call_boundary(state)


if __name__=='__main__':unittest.main()
