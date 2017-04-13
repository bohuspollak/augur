from __future__ import division, print_function
import os, re, time, csv, sys
from io_util import make_dir, remove_dir
# from io_util import myopen, make_dir, remove_dir, tree_to_json, write_json
# from collections import defaultdict
from Bio import SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Alphabet import generic_dna
from Bio.SeqFeature import FeatureLocation
import numpy as np
# from seq_util import pad_nucleotide_sequences, nuc_alpha, aa_alpha
from datetime import datetime
import json
from pdb import set_trace
from utils import fix_names, num_date, ambiguous_date_to_date_range
from pprint import pprint
from Bio import AlignIO
from Bio.Align import MultipleSeqAlignment
import random

class sequence_set(object):

    def __init__(self, logger, sequences, reference, dateFormat):
        super(sequence_set, self).__init__()
        self.log = logger
        self.reference = None
        self.proteins = None

        # load sequences from the (parsed) JSON - don't forget to sort out dates
        self.seqs = {}
        for name, data in sequences.iteritems():
            self.seqs[name] = SeqRecord(Seq(data["seq"], generic_dna),
                   id=name, name=name, description=name)
            self.seqs[name].attributes = data["attributes"]
            # tidy up dates
            self.parse_date(self.seqs[name], dateFormat)

        # load reference from (parsed) JSON & clean up dates
        if reference and len(reference):
            name = reference["attributes"]["strain"]
            self.reference_seq = SeqRecord(Seq(reference["seq"], generic_dna),
                   id=name, name=name, description=name)
            self.reference_seq.attributes = reference["attributes"]
            self.parse_date(self.reference_seq, dateFormat)
            # is reference already in self.seqs?

            #sort out the proteins:
            if "genes" in reference and len(reference["genes"]):
                self.proteins = {k:FeatureLocation(start=v["start"], end=v["end"], strand=v["strand"]) for k, v in reference["genes"].iteritems()}

        # other things:
        self.run_dir = '_'.join(['temp', time.strftime('%Y%m%d-%H%M%S',time.gmtime()), str(random.randint(0,1000000))])
        self.nthreads = 2 # should load from config file

    """ this function is similar to that in sequences_prepare
    these should be consolidated
    """
    def parse_date(self, seq, fmts):
        for fmt in fmts:
            try:
                if 'XX' in seq.attributes['raw_date']:
                    min_date, max_date = ambiguous_date_to_date_range(seq.attributes['raw_date'], fmt)
                    # seq.attributes['raw_date'] = seq.attributes['date']
                    seq.attributes['num_date'] = np.array((num_date(min_date), num_date(max_date)))
                    seq.attributes['date'] = min_date
                else:
                    if callable(fmt):
                        tmp = fmt(seq.attributes['raw_date'])
                    else:
                        try:
                            tmp = datetime.strptime(seq.attributes['raw_date'], fmt).date()
                        except:
                            tmp = seq.attributes['raw_date']
                    # seq.attributes['raw_date'] = seq.attributes['date']
                    seq.attributes['num_date'] = num_date(tmp)
                    seq.attributes['date']=tmp
                    break
            except:
                continue

    def codon_align(self):
        self.log.fatal("Codon align not yet implemented")

    def align(self, debug=False):
        '''
        align sequences using mafft
        '''

        make_dir(self.run_dir)
        os.chdir(self.run_dir)
        ref_in_set = self.reference_seq.name in self.seqs
        if ref_in_set:
            out_seqs = self.seqs.values()
        else:
            out_seqs = self.seqs.values() + [self.reference_seq]
        print("align: reference in set",ref_in_set)
        SeqIO.write(out_seqs, "temp_in.fasta", "fasta")
        os.system("mafft --anysymbol --thread " + str(self.nthreads) + " temp_in.fasta 1> temp_out.fasta 2>mafft_stderr")

        tmp_aln = AlignIO.read('temp_out.fasta', 'fasta')
        self.sequence_lookup = {seq.id:seq for seq in tmp_aln}
        # add attributes to alignment
        for seqid, seq in self.seqs.iteritems():
            self.sequence_lookup[seqid].attributes = seq.attributes
        self.aln = MultipleSeqAlignment([s for s in tmp_aln
                            if s.name!=self.reference_seq.name or ref_in_set])

        os.chdir('..')
        if not debug:
            remove_dir(self.run_dir)

        # # save the alignment!
        # SeqIO.write(self.seqs.aln, self.output_path + "aligned.mfa", "fasta")

    def strip_non_reference(self):
        ungapped = np.array(self.sequence_lookup[self.reference_seq.name])!='-'
        for seq in self.aln:
            seq.seq = Seq("".join(np.array(seq)[ungapped]))

    def remove_terminal_gaps(self):
        for seq in self.aln:
            seq_array = np.array(seq)
            seq_string = str(seq.seq)
            if (seq_array=='-').sum():
                left_gaps = len(seq_string) - len(seq_string.lstrip('-'))
                seq_array[:left_gaps] = 'N'
            if (seq_array=='-').sum():
                right_gaps = len(seq_string) - len(seq_string.rstrip('-'))
                if right_gaps:
                    seq_array[-right_gaps:] = 'N'
            seq.seq = Seq("".join(seq_array))

    def translate(self):
        '''
        make alignment of translations
        '''
        self.translations={}
        if not hasattr(self, "proteins"): # ensure dictionary to hold annotation
            self.proteins={}

        # add a default translation of the entire sequence unless otherwise specified
        if len(self.proteins)==0:
            self.proteins.update({'cds':FeatureLocation(start=0,
                end=self.aln.get_alignment_length(), strand=1)})

        for prot in self.proteins:
            aa_seqs = []
            for seq in self.aln:
                try:
                    # soon not needed as future biopython version will translate --- into -
                    tmpseq = self.proteins[prot].extract(seq)
                    tmpseq.attributes = seq.attributes
                    internal_gap = np.unique(np.where(np.array(tmpseq)=='-')[0]//3)
                    aa_seq = np.array(Seq(str(tmpseq.seq).replace('---', 'NNN')).translate())
                    aa_seq[internal_gap]='-'
                    tmpseq.seq = Seq("".join(aa_seq))
                except:
                    tmpseq.seq = Seq("".join([x if x in 'ACGT' else 'N'
                        for x in str(tmpseq.seq)])).translate()
                    print("Trouble translating",seq.id)
                aa_seqs.append(tmpseq)
            self.translations[prot] = MultipleSeqAlignment(aa_seqs)

    def clock_filter(self, root_seq=None, n_iqd=3, max_gaps = 1.0, plot=False):
        '''
        remove sequences form the set that are that evolve much faster or slower
        compared the majority. Regions with predominantly gaps can be removed since
        this can skew the evolutionary rates.
        '''
        if root_seq is None: # use consensus
            af = calc_af(self.aln, nuc_alpha)   
            root_seq = np.fromstring(nuc_alpha, 'S1')[af.argmax(axis=0)]
        if type(root_seq)==str and root_seq in self.sequence_lookup:
            root_seq = np.array(self.sequence_lookup[root_seq])
        if max_gaps<1.0:
            af=calc_af(self.aln, nuc_alpha)
            good_pos = af[nuc_alpha.index('-')]<max_gaps
        else:
            good_pos = np.ones(self.aln.get_alignment_length(), dtype=bool)
        date_vs_distance = {}
        self.reference_aln = None
        for seq in self.aln:
            date_vs_distance[seq.id] = (seq.attributes['num_date'],
                np.mean((np.array(seq)!=root_seq)[(np.array(seq)!='-')&(root_seq!='-')&good_pos]))
            if seq.id==self.reference.id:
                self.reference_aln = seq
        date_vs_distance_array=np.array(date_vs_distance.values())
        from scipy.stats import linregress, scoreatpercentile
        slope, intercept, rval, pval, stderr = linregress(date_vs_distance_array[:,0], date_vs_distance_array[:,1])
        print("distance vs time regression:",slope)
        residuals = (intercept + slope*date_vs_distance_array[:,0]) - date_vs_distance_array[:,1]
        IQD = scoreatpercentile(residuals, 75) - scoreatpercentile(residuals,25)
        if plot:
            import matplotlib.pyplot as plt
            plt.ion()
            plt.scatter(date_vs_distance_array[:,0], date_vs_distance_array[:,1], c='g')
            bad_points = abs(intercept+slope*date_vs_distance_array[:,0] - date_vs_distance_array[:,1])>n_iqd*IQD
            plt.scatter(date_vs_distance_array[bad_points,0], date_vs_distance_array[bad_points,1], c='r')


        print("before clock filter:",len(self.aln))
        tmp = {seq.id:seq for seq in self.aln
                if abs(intercept+slope*date_vs_distance[seq.id][0] - date_vs_distance[seq.id][1])<n_iqd*IQD}
        if self.reference.id not in tmp and self.reference_aln is not None:
            print('adding reference again after clock filter')
            tmp[self.reference.id] = self.reference_aln
        self.aln = MultipleSeqAlignment(tmp.values())
        print("after clock filter:",len(self.aln))
