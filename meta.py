from __future__ import division
from __future__ import print_function
from abc import ABCMeta, abstractmethod
import numpy
import pprint
import sys
import operator
import logging
import datetime
import copy

import time
import networkx as nx
from csv import reader


def read_matrix(matrix):
    lmatrix = []
    f = open(matrix)
    next(f)
    csv_reader = reader(f)
    for row in csv_reader:
        # logging.info(row)
        lmatrix.append(list(map(str,row)))
    f.close()
    return lmatrix 

class MetaTask(object):

    def __init__(self, tid, comp_cost=[]):
        self.tid = int(tid)# task id - this is unique
        self.rank = -1 # This is updated during the 'Task Prioritisation' phase 
        self.processor = -1
        self.ast = 0 
        self.aft = 0 
        self.scheduled = 0

    def __repr__(self):
        return str(self.tid)
    """
    To utilise the NetworkX graph library, we need to make the node structure hashable  
    Implementation adapted from: http://stackoverflow.com/a/12076539        
    """

    def __hash__(self):
        return hash(self.tid)

    def __eq__(self, task):
        if isinstance(task, self.__class__):
            return self.tid == task.tid
        return NotImplemented

    
    def __lt__(self,task): 
        if isinstance(task,self.__class__):
            return self.tid < task.tid

    def __le__(self,task):
        if isinstance(task,self.__class__):
            return self.tid <= task.tid

class DAG:
    def __init__(self, dag_id, graph, comp, atime, dag_type):

        self.id                 = dag_id
        self.graph              = graph
        self.comp               = comp
        self.arrival_time       = atime
        self.resp_time          = 0
        self.ready_time         = atime
        self.dag_type           = dag_type
        self.completed_peid     = {}


class META:

    E_PWR_MGMT          = 1
    E_TASK_ARRIVAL      = 2
    E_SERVER_FINISHES   = 3
    E_NOTHING           = 4

    E_META_DONE         = 0

    def __init__(self, meta_params, stomp_sim):

        self.params         = meta_params
        self.stomp          = stomp_sim

        self.working_dir                    = self.params['general']['working_dir']
        self.basename                       = self.params['general']['basename']

        self.dag_trace_files                = {}
        self.output_trace_file              = self.params['general']['output_trace_file']
        self.input_trace_file               = self.params['general']['input_trace_file'][1]
        self.stdev_factor                   = self.params['simulation']['stdev_factor']

        self.global_task_trace              = []

        self.dag_dict                       = {}
        self.dag_id_list                    = []
        self.server_types                   = ["cpu_core", "gpu", "accel"]
		
    def run(self):

        ### Read input DAGs ####
        dags_completed = 0
        dags_dropped = 0
        end_list = []

        in_trace_name = self.working_dir + '/' + self.input_trace_file
        logging.info(in_trace_name)

        with open(in_trace_name, 'r') as input_trace:
                line_count = 0;
                for line in input_trace.readlines():
                    tmp = line.strip().split(',')
                    if (line_count >= 0):
                        atime = int(int(tmp.pop(0))*self.params['simulation']['arrival_time_scale'])
                        dag_id = int(tmp.pop(0))
                        dag_type = tmp.pop(0)
                        graph = nx.read_graphml("inputs/random_dag_{0}.graphml".format(dag_type), MetaTask)

                        comp = read_matrix("inputs/random_comp_{0}_{1}.txt".format(dag_type, self.stdev_factor))
                        the_dag_trace = DAG(dag_id, graph, comp, atime, dag_type)
                        self.dag_dict[dag_id] = the_dag_trace
                        self.dag_id_list.append(dag_id)
                    line_count += 1
                    
        while(self.dag_id_list):

            temp_task_trace = []
            completed_list = []

            self.stomp.tlock.acquire()
            while(len(self.stomp.tasks_completed)):
                completed_list.append(self.stomp.tasks_completed.pop(0))
            self.stomp.tlock.release()

            ################# UPDATE BASED ON COMPLETED TASKS #############################
            while (len(completed_list)):
                task_completed = completed_list.pop(0)
                dag_id_completed = task_completed.dag_id

                if dag_id_completed in self.dag_dict:
                    dag_completed = self.dag_dict[dag_id_completed]
                                        
                    for node in dag_completed.graph.nodes():
                        if node.tid == task_completed.tid:
                            dag_completed.ready_time = task_completed.arrival_time + task_completed.task_lifetime
                            dag_completed.resp_time = dag_completed.ready_time - dag_completed.arrival_time

                            dag_completed.graph.remove_node(node)
                            break


                    ## Dag execution completed ##
                    if (len(dag_completed.graph.nodes()) == 0):
                        dags_completed += 1
                        end_entry = (dag_id_completed,dag_completed.dag_type,dag_completed.resp_time)
                        end_list.append(end_entry)
                        # Remove DAG from active list
                        self.dag_id_list.remove(dag_id_completed)
                        del self.dag_dict[dag_id_completed]

            def process_comp_time(dag, tid):
                stimes = []
                count = 0
                # Iterate over each column of comp entry.
                for comp_time in dag.comp[tid]:
                    # Ignore first two columns.
                    if (count <= 1):
                        count += 1
                        continue
                    else:
                        stimes.append((self.server_types[count-2],comp_time))

                    count += 1
                return stimes

            ################# READY TASK PRIORTIZATION AND SUBMISSION #############################
            for dag_id in self.dag_id_list:
                the_dag_sched = self.dag_dict[dag_id]

                ## Push ready tasks into queue
                for node,deg in the_dag_sched.graph.in_degree():
                    # If there are no parents of the node (i.e. it is a root 
                    # node or a node whose dependencies have been satisfied.
                    if deg == 0 and node.scheduled == 0:
                        task_entry = []
                        if (node.tid == 0):
                            atime = the_dag_sched.arrival_time
                        else:
                            atime = the_dag_sched.ready_time
                        task = the_dag_sched.comp[node.tid][0]

                        ## Ready task found, push into task queue
                        stimes = []
                        stimes = process_comp_time(the_dag_sched, node.tid)

                        task_entry.append((atime, task, dag_id, node.tid))
                        
                        task_entry.append(stimes)
                        temp_task_trace.append(task_entry)
                        node.scheduled = 1
            
            self.stomp.lock.acquire()
            while (len(temp_task_trace)):
                self.stomp.global_task_trace.append(temp_task_trace.pop(0))
                self.stomp.global_task_trace.sort(key=lambda tr_entry: tr_entry[0][0], reverse=False)


            if (len(self.stomp.global_task_trace) and (self.stomp.next_cust_arrival_time != self.stomp.global_task_trace[0][0][0])):
                self.stomp.next_cust_arrival_time = self.stomp.global_task_trace[0][0][0]

            
            self.stomp.tlock.acquire()
            if(self.stomp.task_completed_flag == 1 and (len(self.stomp.tasks_completed) == 0)):
                self.stomp.task_completed_flag = 0
            self.stomp.tlock.release()


            self.stomp.lock.release()   

            self.stomp.E_META_START = 1
            self.stomp.stats['Tasks Generated by META'] = 1
                

        # META completed
        self.stomp.E_META_DONE = 1

        fho = open(self.params['general']['working_dir'] + '/out.csv', 'w')        
        fho.write('DAG ID,DAG Type,Response Time\n')
        

        end_list.sort(key=lambda end_entry: end_entry[0], reverse=False)
        while(len(end_list)):
            end_entry = end_list.pop(0)
            fho.write(str(end_entry[0]) + ',' + str(end_entry[1]) + ',' + str(end_entry[2]) + '\n')
            # end_entry = (dag_id_completed,dag_completed.dag_type,dag_completed.resp_time)
        
        fho.close()


