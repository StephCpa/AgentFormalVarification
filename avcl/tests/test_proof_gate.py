import unittest
from scripts.verify import check_lean_axioms

class ProofGateTests(unittest.TestCase):
    def test_standard_dependencies_and_axiom_free_theorem_pass(self):
        output="'A' depends on axioms: [propext, Quot.sound]\n'B' does not depend on any axioms\n"
        report=check_lean_axioms(output,("A","B"))
        self.assertTrue(report["passed"]);self.assertEqual(report["theorems"]["B"],[])

    def test_incidental_name_or_missing_declaration_cannot_pass(self):
        for output in ("error: A could not be proved","#check A","'Other' does not depend on any axioms"):
            report=check_lean_axioms(output,("A",))
            self.assertFalse(report["passed"]);self.assertEqual(report["missing"],["A"])
        self.assertFalse(check_lean_axioms("",())["passed"])

    def test_placeholder_and_custom_axiom_cannot_pass(self):
        for dependency in ("sorryAx","UnprovedAdapterSoundness"):
            report=check_lean_axioms("'A' depends on axioms: [propext, "+dependency+"]",("A",))
            self.assertFalse(report["passed"]);self.assertIn(dependency,report["unexpected_axioms"]["A"])

    def test_unrelated_placeholder_in_module_also_fails(self):
        output="'A' does not depend on any axioms\n'Other' depends on axioms: [sorryAx]\n"
        self.assertFalse(check_lean_axioms(output,("A",))["passed"])

    def test_duplicate_and_malformed_dependency_reports_fail(self):
        line="'A' does not depend on any axioms"
        self.assertFalse(check_lean_axioms(line+"\n"+line,("A",))["passed"])
        self.assertFalse(check_lean_axioms("'A' depends on axioms: [propext",("A",))["passed"])

if __name__=="__main__":unittest.main()
