"""Search enumerates source choices; missing semantics are never guessed."""
import unittest
from pm2_animation_lab.pm2_activity_timeline import RandomTape, MissingInputError, DeterminismError
from pm2_animation_lab.pm2_activity_condition_search import enumerate_tapes, infer


class SourceDomainTests(unittest.TestCase):
    def test_search_rejects_non_D_quarantine_before_reading_inputs(self):
        with self.assertRaisesRegex(ValueError,'existing_data_and_source_directories_required'):
            infer('missing.json','missing-output','C:/pm2_activity_runtime','D:/fixed-source')

    def test_search_rejects_non_D_source_before_reading_inputs(self):
        with self.assertRaisesRegex(ValueError,'existing_data_and_source_directories_required'):
            infer('missing.json','missing-output','D:/pm2_activity_runtime','C:/fixed-source')

    def test_conditional_rng_tree_preserves_each_source_domain(self):
        def execute(values):
            tape=RandomTape(values)
            a=tape.consume(2,'test',1)
            b=tape.consume(3,'test',2) if a==2 else None
            tape.require_exhausted();return a,b
        self.assertEqual(list(enumerate_tapes(execute)),[([1],(1,None)),([2,1],(2,1)),([2,2],(2,2)),([2,3],(2,3))])

    def test_non_rng_missing_inputs_are_not_silently_filled(self):
        def execute(values):raise MissingInputError('unknown_script_variable')
        with self.assertRaisesRegex(MissingInputError,'unknown_script_variable'):
            list(enumerate_tapes(execute))

    def test_large_unreviewed_rng_domain_fails_closed(self):
        def execute(values):return RandomTape(values).consume(10000,'test',1)
        with self.assertRaisesRegex(ValueError,'unreviewed_random_domain'):
            list(enumerate_tapes(execute))

    def test_search_bound_does_not_claim_exhaustive_result(self):
        def execute(values):return RandomTape(values).consume(3,'test',1)
        with self.assertRaisesRegex(ValueError,'random_domain_limit'):
            list(enumerate_tapes(execute,limit=2))

    def test_interpreter_determinism_errors_propagate(self):
        def execute(values):raise DeterminismError('invalid_state')
        with self.assertRaises(DeterminismError):list(enumerate_tapes(execute))


if __name__=='__main__':unittest.main()
